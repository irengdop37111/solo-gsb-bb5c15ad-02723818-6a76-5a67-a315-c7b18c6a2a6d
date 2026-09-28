"""日历事件提取（``GET /calendar/events``，iCalendar / RFC 5545）。

在**全部已归档邮件**的 MIME 树上找出每个
``Content-Type: text/calendar`` 叶子（含 ``message/rfc822`` /
``message/global`` 封装报文内部），从其归档时解码、NFC 规范化、
换行统一为 ``\\n`` 的文本中解析**单次 VEVENT**。本模块是**纯只读**
的确定性解析：输入为 :func:`database.list_calendar_nodes` 一类查询
返回的节点行（``text_content`` 即解码后的日历文本），不回写任何记录，
对历史归档数据同样适用。

解析规则（RFC 5545 / RFC 6047）：

* **展开折叠行**：物理行以 ``CRLF`` / ``LF`` / ``CR`` 后跟空格或制表符
  为折叠点，按 RFC 5545 §3.1 去折叠——删去行终止符及其后**恰好一个**
  WSP；首个逻辑行以 WSP 开头（无可并入行）记为行级结构诊断；
* 顶层取日历 **METHOD**（``REQUEST`` / ``CANCEL`` 等，规范化大写），
  仅识别直接处于 ``VCALENDAR`` 内的 ``METHOD`` 属性，方法缺失为
  ``None``（不诊断）；``VTIMEZONE`` 与 ``VALARM`` 内部内容不参与事件
  提取；
* 每个 ``VEVENT`` 读取 ``UID``、``SEQUENCE``（缺省 0，必须为非负整数）、
  ``DTSTAMP``、``DTSTART``、``DTEND``、``SUMMARY``；单值属性重复出现
  记诊断并令该事件无效；
* 事件必须具备非空 ``UID``、``DTSTAMP``、``DTSTART``、``DTEND``；
  时间只接受 **UTC**（``...Z``）或 **IANA ``TZID``** 两种形式——浮动时间
  、纯日期（``VALUE=DATE``）、未知 TZID 均逐项诊断；IANA 时间按其时区
  规则转换为 UTC，落入夏令时**间隙**（不存在时间）或**重叠歧义**
  （回拨时段被表述两次）同样逐项诊断；
* **同一节点内重复 UID**：该 UID 的每个 VEVENT 逐项诊断，且它们都不
  参与跨邮件版本选择（不同节点间的同 UID 是正常的会议更新，不在此列）。

跨邮件同 UID 的版本选择（只在有效事件间进行）：依次比较
``SEQUENCE``（大者胜）、``DTSTAMP``（晚者胜）、``message_id``
（大者胜，确定性兜底）；``METHOD:CANCEL`` 的当选事件标记
``cancelled=true`` 并保留全部来源字段。结果按
``(DTSTART UTC, UID)`` 稳定排序。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---------------------------------------------------------------------------
# 诊断码（稳定的机器可读码）
# ---------------------------------------------------------------------------

CODE_CALENDAR_MALFORMED = "CALENDAR_MALFORMED"
CODE_CALENDAR_INVALID_LINE = "CALENDAR_INVALID_LINE"
CODE_MISSING_EVENT_FIELD = "MISSING_EVENT_FIELD"
CODE_INVALID_EVENT_FIELD = "INVALID_EVENT_FIELD"
CODE_INVALID_EVENT_TIME = "INVALID_EVENT_TIME"
CODE_DUPLICATE_UID_IN_NODE = "DUPLICATE_UID_IN_NODE"

# iCalendar 内容行：属性名 + 分号参数表 + 冒号 + 值。
_NAME_RE = re.compile(r"^[-A-Za-z0-9]+$")
# RFC 5545 §3.3.5：date-time = date T time [time-zone / Z]
_DATE_TIME_RE = re.compile(
    r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z)?$"
)
_DATE_RE = re.compile(r"^\d{8}$")

# 事件上单值、重复出现即视为损坏的属性。
_SINGLE_VALUED = frozenset(
    {"uid", "sequence", "dtstamp", "dtstart", "dtend", "summary"}
)
_REQUIRED_FIELDS = ("uid", "dtstamp", "dtstart", "dtend")


# ---------------------------------------------------------------------------
# 结果数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventDiagnostic:
    """一条逐项诊断，附其归档定位。

    ``order`` 为按扫描顺序分配的全局序号，供最终稳定排序兜底。
    """

    code: str
    message_id: int
    node_path: str
    detail: dict
    order: int


@dataclass
class RawEvent:
    """解析阶段的单个 VEVENT（尚未判定有效性）。"""

    message_id: int
    node_path: str
    method: Optional[str]  # 所属 VCALENDAR 的 METHOD（大写），无则 None
    index: int  # 节点内 VEVENT 的文档序（0 基）
    props: dict[str, tuple[str, dict[str, str]]] = field(default_factory=dict)
    invalid: bool = False  # 已命中重复单值属性等行级问题


@dataclass(frozen=True)
class CalendarEvent:
    """跨邮件版本选择后的当前版本事件（时间均为 UTC）。"""

    uid: str
    sequence: int
    method: Optional[str]
    cancelled: bool
    dtstamp: datetime
    dtstart: datetime
    dtend: datetime
    summary: Optional[str]
    message_id: int
    node_path: str


@dataclass
class NodeCalendarResult:
    """一个 text/calendar 节点的全部 VEVENT 与节点级诊断。"""

    message_id: int
    node_path: str
    events: list[RawEvent] = field(default_factory=list)
    diagnostics: list[EventDiagnostic] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 折叠行展开与内容行切分（RFC 5545 §3.1）
# ---------------------------------------------------------------------------


def unfold_lines(text: str) -> tuple[list[str], bool]:
    """展开 iCalendar 折叠行，返回 ``(逻辑行列表, 是否出现块首折叠)``。

    归档阶段换行已统一为 ``\\n``，这里仍对 CRLF / 裸 CR 保持对称容忍。
    折叠点（行终止符后紧跟 WSP）按 RFC 删除行终止符及**恰好一个** WSP，
    其余空白保留；第一个逻辑行以 WSP 开头时无法并入任何行，标记为
    结构错误，该行的首字符 WSP 原样保留以继续解析。
    """

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    logical: list[str] = []
    leading_fold = False
    for line in text.split("\n"):
        if line[:1] in (" ", "\t"):
            if not logical:
                leading_fold = True
                logical.append(line)
            else:
                # RFC 5545 §3.1：折叠点的 CRLF 及其后恰好一个线性空白
                # 字符被移除；若还有额外空白，保留其余部分。
                logical[-1] += line[1:]
        else:
            logical.append(line)
    return logical, leading_fold


def split_content_line(
    line: str,
) -> Optional[tuple[str, dict[str, str], str]]:
    """把一条内容行拆成 ``(属性名, 参数表, 原始值)``。

    参数解析识别双引号字符串（引号内 ``;`` / ``:`` 为字面量、
    ``\\`` / ``"`` 转义为字符本身）；参数名大写归一，同名参数重复或
    属性名非法返回 ``None``（调用方记 :data:`CODE_CALENDAR_INVALID_LINE`）。
    """

    name_end = 0
    n = len(line)
    while name_end < n and line[name_end] not in (";", ":"):
        name_end += 1
    name = line[:name_end]
    if not name or not _NAME_RE.match(name):
        return None

    params: dict[str, str] = {}
    i = name_end
    while i < n and line[i] == ";":
        i += 1
        pstart = i
        while i < n and line[i] != "=" and line[i] != ":" and line[i] != ";":
            i += 1
        if i >= n or line[i] != "=":
            return None
        pname = line[pstart:i]
        if not pname or not _NAME_RE.match(pname):
            return None
        i += 1  # 跳过 '='
        if i < n and line[i] == '"':
            i += 1
            chars: list[str] = []
            closed = False
            while i < n:
                ch = line[i]
                if ch == "\\":
                    # 引号内反斜杠转义下一个字符（RFC 5545 §3.1）。
                    if i + 1 < n:
                        chars.append(line[i + 1])
                        i += 2
                        continue
                    chars.append(ch)
                    i += 1
                    continue
                if ch == '"':
                    closed = True
                    i += 1
                    break
                chars.append(ch)
                i += 1
            if not closed:
                return None
            pvalue = "".join(chars)
        else:
            vstart = i
            while i < n and line[i] != ";" and line[i] != ":":
                i += 1
            pvalue = line[vstart:i]
        key = pname.upper()
        if key in params:
            return None  # 同名参数重复
        params[key] = pvalue
        # 参数后必须是 ';'（继续）或 ':'（进入值）。
        if i < n and line[i] == ";":
            continue
        break

    if i >= n or line[i] != ":":
        return None
    return name.upper(), params, line[i + 1:]


# ---------------------------------------------------------------------------
# 时间解析（UTC 或 IANA TZID）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimeProblem(Exception):
    """时间值解析失败（携带原因码与细节），内部控制流用。"""

    code: str
    reason: str
    detail: dict

    def __str__(self) -> str:  # 便于测试/日志直接打印
        return f"{self.code}: {self.reason} {self.detail}"


def _build_naive(value: str, field: str) -> datetime:
    m = _DATE_TIME_RE.match(value)
    if not m:
        raise TimeProblem(
            CODE_INVALID_EVENT_TIME,
            "not_iso",
            {"field": field, "value": value},
        )
    year, month, day, hour, minute, second = (int(g) for g in m.groups()[:6])
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError as exc:
        raise TimeProblem(
            CODE_INVALID_EVENT_TIME,
            "not_a_datetime",
            {"field": field, "value": value, "reason_text": str(exc)},
        ) from None


def _dst_status(naive: datetime, tz: ZoneInfo) -> str:
    """判定朴素时间在某 IANA 时区的夏令时状态。

    * ``ok``：恰好对应一个 UTC 时刻；
    * ``gap``：落入春季跳时间隙（墙上时钟不存在该时刻）；
    * ``ambiguous``：落入秋季回拨重叠（同一墙上时间发生两次）。

    依据 PEP 495 的 ``fold`` 语义：间隙中 ``fold=0`` 的偏移较小且
    往返不闭合；重叠中两种 fold 的 UTC 偏移不同。
    """

    f0 = naive.replace(tzinfo=tz, fold=0)
    f1 = naive.replace(tzinfo=tz, fold=1)
    off0 = f0.utcoffset()
    off1 = f1.utcoffset()
    assert off0 is not None and off1 is not None
    if off0 == off1:
        # 偏移相同可能仍是间隙（fold=0 落在过渡前规则上的特殊情形），
        # 用往返闭合性最终判定。
        rt = f0.astimezone(ZoneInfo("UTC")).astimezone(tz)
        if rt.replace(tzinfo=None) != naive or rt.fold != f0.fold:
            return "gap"
        return "ok"
    # 偏移不同：fold=0 偏移较小（春季跳时被跳过）为间隙，否则为重叠。
    return "gap" if off0 < off1 else "ambiguous"


def parse_event_time(
    value: str, params: dict[str, str], field: str
) -> datetime:
    """把 DTSTART/DTEND/DTSTAMP 的值解析为 UTC ``datetime``。

    仅接受 UTC（``Z`` 后缀）或带 IANA ``TZID`` 的本地时间；浮动时间、
    纯日期、未知/非 IANA 时区、夏令时间隙与重叠一律抛
    :class:`TimeProblem`。
    """

    value_type = params.get("VALUE", "").upper()
    if value_type == "DATE" or (_DATE_RE.match(value) and "TZID" not in params):
        raise TimeProblem(
            CODE_INVALID_EVENT_TIME,
            "date_not_datetime",
            {"field": field, "value": value},
        )

    m = _DATE_TIME_RE.match(value)
    if m is None:
        raise TimeProblem(
            CODE_INVALID_EVENT_TIME,
            "not_iso",
            {"field": field, "value": value},
        )
    is_utc_suffix = bool(m.group(7))
    tzid = params.get("TZID")
    naive = _build_naive(value, field)

    if is_utc_suffix:
        if tzid is not None:
            raise TimeProblem(
                CODE_INVALID_EVENT_TIME,
                "utc_with_tzid",
                {"field": field, "value": value, "tzid": tzid},
            )
        return naive.replace(tzinfo=None)  # 朴素 UTC，输出时再附 tzinfo

    if tzid is None:
        # 无 Z 也无 TZID：浮动时间，无法确定地转换为 UTC。
        raise TimeProblem(
            CODE_INVALID_EVENT_TIME,
            "floating_time",
            {"field": field, "value": value},
        )

    # / 前缀是 RFC 5545 的唯一时区引用形式；本实现只接受 IANA 库可直接
    # 解析的名称（系统 tzdata 或 pip 包 tzdata），别名解析交给 zoneinfo。
    try:
        tz = ZoneInfo(tzid)
    except ZoneInfoNotFoundError:
        raise TimeProblem(
            CODE_INVALID_EVENT_TIME,
            "unknown_tzid",
            {"field": field, "value": value, "tzid": tzid},
        ) from None

    status = _dst_status(naive, tz)
    if status == "gap":
        raise TimeProblem(
            CODE_INVALID_EVENT_TIME,
            "dst_gap",
            {"field": field, "value": value, "tzid": tzid},
        )
    if status == "ambiguous":
        raise TimeProblem(
            CODE_INVALID_EVENT_TIME,
            "dst_ambiguous",
            {"field": field, "value": value, "tzid": tzid},
        )
    return naive.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(
        tzinfo=None
    )


# ---------------------------------------------------------------------------
# RFC 5545 TEXT 反转义（§3.3.11）
# ---------------------------------------------------------------------------


def unescape_text(value: str) -> str:
    out: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch == "\\" and i + 1 < n:
            nxt = value[i + 1]
            out.append({"n": "\n", "N": "\n"}.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# 节点解析
# ---------------------------------------------------------------------------


@dataclass
class _Frame:
    component: str
    method: Optional[str] = None  # 仅 VCALENDAR 帧使用


def parse_calendar_node(row: sqlite3.Row, counter: list[int]) -> NodeCalendarResult:
    """解析单个节点行，产出其中的全部 VEVENT 与逐项诊断。

    ``counter`` 为单元素列表，跨节点单调递增，为诊断分配稳定序号。
    """

    message_id = int(row["message_id"])
    node_path = row["path"]
    result = NodeCalendarResult(message_id=message_id, node_path=node_path)

    def diag(code: str, detail: dict) -> None:
        result.diagnostics.append(
            EventDiagnostic(
                code=code,
                message_id=message_id,
                node_path=node_path,
                detail=detail,
                order=counter[0],
            )
        )
        counter[0] += 1

    text = row["text_content"]
    if text is None or not text.strip():
        diag(
            CODE_CALENDAR_MALFORMED,
            {"reason": "empty_body"},
        )
        return result

    lines, leading_fold = unfold_lines(text)
    if leading_fold:
        diag(
            CODE_CALENDAR_MALFORMED,
            {"reason": "fold_without_previous_line"},
        )

    stack: list[_Frame] = []
    current: Optional[RawEvent] = None
    event_index = 0
    saw_vcalendar = False

    def close_event() -> None:
        nonlocal current, event_index
        if current is not None:
            result.events.append(current)
            event_index += 1
            current = None

    for raw_line in lines:
        if raw_line == "":
            # 日历内容不使用空行作为结构；忽略（归档前导/尾随空行）。
            continue
        parsed = split_content_line(raw_line)
        if parsed is None:
            if not stack or stack[-1].component not in ("vcalendar", "vevent"):
                # VTIMEZONE / VALARM / 未知组件内部不做严格校验。
                continue
            if current is not None:
                current.invalid = True
            diag(
                CODE_CALENDAR_INVALID_LINE,
                {"line": raw_line[:120]},
            )
            continue

        name, _params, value = parsed

        if name == "BEGIN":
            comp = value.upper()
            if comp == "VEVENT":
                if any(f.component.startswith("vevent") for f in stack):
                    # 嵌套 VEVENT 违反 RFC 5545（外层事件此刻必存在）：
                    # 外层事件标记作废，内层内容进入 vevent-nested 帧，
                    # 其属性不收集，END:VEVENT 时弹出并回到外层帧。
                    if current is not None:
                        current.invalid = True
                    diag(
                        CODE_CALENDAR_MALFORMED,
                        {"reason": "nested_vevent"},
                    )
                    stack.append(_Frame("vevent-nested"))
                    continue
                method = next(
                    (f.method for f in reversed(stack)
                     if f.component == "vcalendar"),
                    None,
                )
                current = RawEvent(
                    message_id=message_id,
                    node_path=node_path,
                    method=method,
                    index=event_index,
                )
            elif comp == "VCALENDAR":
                saw_vcalendar = True
            stack.append(_Frame(comp.lower()))
            continue

        if name == "END":
            comp = value.upper()
            top_comp = stack[-1].component if stack else None
            # 嵌套 VEVENT 帧只由 END:VEVENT 关闭（帧名是解析器内部别名）。
            if top_comp == "vevent-nested":
                if comp != "VEVENT":
                    diag(
                        CODE_CALENDAR_MALFORMED,
                        {
                            "reason": "end_without_matching_begin",
                            "component": value,
                        },
                    )
                    continue
                stack.pop()
                continue
            if not stack or top_comp != comp.lower():
                diag(
                    CODE_CALENDAR_MALFORMED,
                    {
                        "reason": "end_without_matching_begin",
                        "component": value,
                    },
                )
                continue
            if top_comp == "vevent":
                close_event()
            stack.pop()
            continue

        # 普通属性行：只收集直接处于 VCALENDAR 层的 METHOD 与直接处于
        # VEVENT 层的属性；VALARM / VTIMEZONE 等内层组件的属性忽略。
        top = stack[-1].component if stack else None
        if current is not None and top == "vevent":
            lname = name.lower()
            if lname in _SINGLE_VALUED:
                if lname in current.props:
                    current.invalid = True
                    diag(
                        CODE_INVALID_EVENT_FIELD,
                        {
                            "reason": "duplicate_property",
                            "property": lname.upper(),
                            "event_index": current.index,
                        },
                    )
                else:
                    current.props[lname] = (value, _params)
        elif stack and stack[-1].component == "vcalendar" and name == "METHOD":
            method = value.strip().upper()
            if stack[-1].method is not None:
                diag(
                    CODE_CALENDAR_MALFORMED,
                    {"reason": "duplicate_method"},
                )
            else:
                stack[-1].method = method

    if current is not None or stack:
        diag(
            CODE_CALENDAR_MALFORMED,
            {"reason": "unclosed_component"},
        )
        # 未闭合的 VEVENT 不进入结果：其属性集不完整，缺字段诊断只会
        # 重复噪音；结构诊断已定位节点。
        current = None
    if not saw_vcalendar:
        diag(
            CODE_CALENDAR_MALFORMED,
            {"reason": "no_vcalendar"},
        )
    return result


# ---------------------------------------------------------------------------
# 事件定稿（必填字段 + 时间转换）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidEvent:
    """通过字段/时间校验的事件（时间为朴素 UTC）。"""

    raw: RawEvent
    uid: str
    sequence: int
    dtstamp: datetime
    dtstart: datetime
    dtend: datetime
    summary: Optional[str]


def finalize_events(
    node_result: NodeCalendarResult,
    diagnostics: list[EventDiagnostic],
    counter: list[int],
) -> list[ValidEvent]:
    """校验单个节点结果中的 VEVENT，返回有效事件。

    新增诊断按扫描顺序追加进跨节点共享的 ``diagnostics``。
    """

    valid: list[ValidEvent] = []

    def diag(code: str, detail: dict) -> None:
        diagnostics.append(
            EventDiagnostic(
                code=code,
                message_id=node_result.message_id,
                node_path=node_result.node_path,
                detail=detail,
                order=counter[0],
            )
        )
        counter[0] += 1

    for raw in node_result.events:
        new_diag_count_before = len(diagnostics)

        def emit(code: str, detail: dict) -> None:
            diag(code, detail)

        if raw.invalid:
            # 行级问题（重复单值属性、嵌套 VEVENT 等）已诊断；该事件
            # 整体作废，不再逐项检查字段以免重复噪音。
            continue

        props = raw.props

        def field_value(fname: str) -> Optional[tuple[str, dict[str, str]]]:
            return props.get(fname)

        missing = [
            f.upper()
            for f in _REQUIRED_FIELDS
            if not (field_value(f) and field_value(f)[0].strip())
        ]
        if missing:
            emit(
                CODE_MISSING_EVENT_FIELD,
                {
                    "reason": "missing_required",
                    "fields": missing,
                    "event_index": raw.index,
                },
            )

        uid_value = field_value("uid")
        uid = uid_value[0].strip() if uid_value and uid_value[0].strip() else None

        sequence = 0
        seq_entry = field_value("sequence")
        if seq_entry is not None and seq_entry[0].strip():
            seq_text = seq_entry[0].strip()
            if not re.fullmatch(r"\d+", seq_text):
                emit(
                    CODE_INVALID_EVENT_FIELD,
                    {
                        "reason": "sequence_not_integer",
                        "property": "SEQUENCE",
                        "value": seq_text,
                        "event_index": raw.index,
                    },
                )
            else:
                sequence = int(seq_text)

        times: dict[str, datetime] = {}
        for fname in ("dtstamp", "dtstart", "dtend"):
            entry = field_value(fname)
            if entry is None or not entry[0].strip():
                continue
            value, tparams = entry
            try:
                times[fname] = parse_event_time(
                    value.strip(), tparams, fname.upper()
                )
            except TimeProblem as problem:
                emit(
                    problem.code,
                    {"reason": problem.reason, **problem.detail},
                )

        if len(diagnostics) > new_diag_count_before:
            continue

        summary_entry = field_value("summary")
        summary = (
            unescape_text(summary_entry[0]) if summary_entry is not None else None
        )

        assert uid is not None
        valid.append(
            ValidEvent(
                raw=raw,
                uid=uid,
                sequence=sequence,
                dtstamp=times["dtstamp"],
                dtstart=times["dtstart"],
                dtend=times["dtend"],
                summary=summary,
            )
        )

    return valid


# ---------------------------------------------------------------------------
# 跨邮件聚合
# ---------------------------------------------------------------------------


def extract_calendar_events(
    rows: list[sqlite3.Row],
) -> tuple[list[CalendarEvent], list[EventDiagnostic]]:
    """在全部归档邮件的日历节点行上提取当前版本事件与全部诊断。

    ``rows`` 需含 ``message_id``、``path``、``text_content`` 列；行序
    无关，内部按 ``(message_id, path)`` 处理。诊断稳定按
    ``(message_id, node_path, order, code)`` 排序。
    """

    counter = [0]
    all_valid: list[ValidEvent] = []
    diagnostics: list[EventDiagnostic] = []

    ordered_rows = sorted(
        rows,
        key=lambda r: (
            int(r["message_id"]),
            tuple(int(s) for s in r["path"].split("/")) if r["path"] else (),
        ),
    )

    # 同一节点内重复 UID：每个出现项逐项诊断并整体排除出跨邮件选择。
    for row in ordered_rows:
        node_result = parse_calendar_node(row, counter)
        # parse_calendar_node 内部产生的节点级诊断并入跨节点列表。
        diagnostics.extend(node_result.diagnostics)
        valid = finalize_events(node_result, diagnostics, counter)

        seen: dict[str, int] = {}
        duplicated: set[str] = set()
        for ev in valid:
            if ev.uid in seen:
                duplicated.add(ev.uid)
                seen[ev.uid] += 1
            else:
                seen[ev.uid] = 1
        if duplicated:
            # 先按事件文档序逐项登记，保证“逐项诊断”顺序稳定。
            for ev in valid:
                if ev.uid in duplicated:
                    diagnostics.append(
                        EventDiagnostic(
                            code=CODE_DUPLICATE_UID_IN_NODE,
                            message_id=node_result.message_id,
                            node_path=node_result.node_path,
                            detail={
                                "uid": ev.uid,
                                "event_index": ev.raw.index,
                                "occurrences": seen[ev.uid],
                            },
                            order=counter[0],
                        )
                    )
                    counter[0] += 1
            valid = [ev for ev in valid if ev.uid not in duplicated]
        all_valid.extend(valid)

    # 跨邮件（及跨节点）同 UID：SEQUENCE → DTSTAMP → message_id 选当前。
    chosen: dict[str, ValidEvent] = {}
    for ev in all_valid:
        incumbent = chosen.get(ev.uid)
        if incumbent is None or (
            ev.sequence,
            ev.dtstamp,
            ev.raw.message_id,
        ) > (
            incumbent.sequence,
            incumbent.dtstamp,
            incumbent.raw.message_id,
        ):
            chosen[ev.uid] = ev

    events: list[CalendarEvent] = []
    for ev in chosen.values():
        method = ev.raw.method
        events.append(
            CalendarEvent(
                uid=ev.uid,
                sequence=ev.sequence,
                method=method,
                cancelled=(method == "CANCEL"),
                dtstamp=ev.dtstamp,
                dtstart=ev.dtstart,
                dtend=ev.dtend,
                summary=ev.summary,
                message_id=ev.raw.message_id,
                node_path=ev.raw.node_path,
            )
        )
    events.sort(key=lambda e: (e.dtstart, e.uid))

    diagnostics.sort(
        key=lambda d: (d.message_id, _path_tuple(d.node_path), d.order, d.code)
    )
    return events, diagnostics


def _path_tuple(path: str) -> tuple[int, ...]:
    if not path:
        return ()
    return tuple(int(seg) for seg in path.split("/"))
