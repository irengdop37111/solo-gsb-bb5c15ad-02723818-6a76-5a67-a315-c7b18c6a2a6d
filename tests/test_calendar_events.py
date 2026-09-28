"""日历事件聚合（GET /calendar/events，RFC 5545 iCalendar）测试。

覆盖：text/calendar 节点识别（含封装报文内部）、折叠行展开、METHOD/
UID/SEQUENCE/DTSTAMP/DTSTART/DTEND/SUMMARY 读取、UTC 与 IANA TZID
转 UTC、浮动时间/纯日期/未知 TZID/夏令时间隙与重叠逐项诊断、缺必填
字段、SEQUENCE 缺省 0/非法、SUMMARY 反转义、同节点重复 UID、跨邮件
SEQUENCE→DTSTAMP→message_id 版本选择、CANCEL 标记与来源保留、
(dtstart, uid) 稳定排序、空归档、base64 CTE、纯只读不改写。
"""

import base64

from conftest import make_multipart


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def ical_text(
    events,
    *,
    method: str | None = "REQUEST",
    extra_calendar_lines: list[str] | None = None,
) -> str:
    if isinstance(events, str):
        events = [events]
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Test//EN"]
    if method is not None:
        lines.append(f"METHOD:{method}")
    if extra_calendar_lines:
        lines.extend(extra_calendar_lines)
    lines.extend(events)
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def vevent(
    uid: str = "event-1@example.com",
    *,
    sequence: str | int | None = 0,
    dtstamp: str = "20260920T080000Z",
    dtstart: str = "20261001T090000Z",
    dtend: str = "20261001T100000Z",
    summary: str | None = "Team standup",
    start_params: str = "",
    end_params: str = "",
    stamp_params: str = "",
    extra: list[str] | None = None,
) -> str:
    lines = ["BEGIN:VEVENT"]
    lines.append(f"UID:{uid}")
    if sequence is not None:
        lines.append(f"SEQUENCE:{sequence}")
    lines.append(f"DTSTAMP{stamp_params}:{dtstamp}")
    lines.append(f"DTSTART{start_params}:{dtstart}")
    lines.append(f"DTEND{end_params}:{dtend}")
    if summary is not None:
        lines.append(f"SUMMARY:{summary}")
    if extra:
        lines.extend(extra)
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


def calendar_part(
    text: str,
    *,
    cte: str | None = None,
    charset: str | None = None,
    content_type: str = "text/calendar",
) -> bytes:
    # 注意：make_multipart 会在部分末尾补一个 CRLF 分隔符，故正文本身
    # 不带尾随换行，避免双换行被解析器当作裸 CR。
    ct = f"Content-Type: {content_type}".encode()
    if charset:
        ct += f"; charset={charset}".encode()
    heads = [ct, b"Content-Transfer-Encoding: base64"] if cte == "base64" else [ct]
    if cte and cte != "base64":
        heads.append(f"Content-Transfer-Encoding: {cte}".encode())
    body = text.rstrip("\r\n").encode(charset or "utf-8")
    if cte == "base64":
        body = base64.b64encode(body)
    return b"\r\n".join(heads) + b"\r\n\r\n" + body


def text_part(text: str = "please see calendar") -> bytes:
    return (
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + text.encode()
    )


def mail(*parts: bytes, subject: str = "Meeting", boundary: str = "MIX") -> bytes:
    # 固定在首位放一个 text/plain 正文，使日历部分稳定落在节点 "2"，
    # 贴合真实邀请邮件（multipart/mixed 或 alternative 包裹 ics）。
    return (
        f"Subject: {subject}\r\n".encode()
        + f"Content-Type: multipart/mixed; boundary={boundary}\r\n\r\n".encode()
        + make_multipart([text_part(), *parts], boundary)
    )


def post(client, data: bytes) -> int:
    resp = client.post(
        "/messages",
        content=data,
        headers={"Content-Type": "message/rfc822"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def events_response(client):
    resp = client.get("/calendar/events")
    assert resp.status_code == 200, resp.text
    return resp.json()


def diag_codes(payload) -> list[str]:
    return [d["code"] for d in payload["diagnostics"]]


def find_diag(payload, code: str, **detail_subset):
    for d in payload["diagnostics"]:
        if d["code"] != code:
            continue
        if all(d["detail"].get(k) == v for k, v in detail_subset.items()):
            return d
    return None


# ---------------------------------------------------------------------------
# 基础提取
# ---------------------------------------------------------------------------


def test_basic_utc_event(client):
    post(client, mail(calendar_part(ical_text(vevent()))))
    payload = events_response(client)
    assert payload["diagnostics"] == []
    assert len(payload["events"]) == 1
    ev = payload["events"][0]
    assert ev == {
        "uid": "event-1@example.com",
        "sequence": 0,
        "method": "REQUEST",
        "cancelled": False,
        "dtstamp": "2026-09-20T08:00:00Z",
        "dtstart": "2026-10-01T09:00:00Z",
        "dtend": "2026-10-01T10:00:00Z",
        "summary": "Team standup",
        "message_id": 1,
        "node_path": "2",
    }


def test_sequence_defaults_to_zero_and_missing_summary(client):
    post(
        client,
        mail(calendar_part(ical_text(vevent(sequence=None, summary=None)))),
    )
    payload = events_response(client)
    assert payload["diagnostics"] == []
    ev = payload["events"][0]
    assert ev["sequence"] == 0
    assert ev["summary"] is None


def test_method_case_normalized_but_other_methods_kept(client):
    post(client, mail(calendar_part(ical_text(vevent(), method="request"))))
    payload = events_response(client)
    assert payload["events"][0]["method"] == "REQUEST"
    assert payload["events"][0]["cancelled"] is False


def test_method_absent_is_none(client):
    post(client, mail(calendar_part(ical_text(vevent(), method=None))))
    payload = events_response(client)
    assert payload["diagnostics"] == []
    assert payload["events"][0]["method"] is None
    assert payload["events"][0]["cancelled"] is False


def test_no_calendar_nodes_empty(client):
    post(client, mail(text_part()))
    payload = events_response(client)
    assert payload == {"events": [], "diagnostics": []}


def test_empty_archive(client):
    payload = events_response(client)
    assert payload == {"events": [], "diagnostics": []}


# ---------------------------------------------------------------------------
# 折叠行
# ---------------------------------------------------------------------------


def test_folded_lines_unfolded(client):
    # RFC 5545：CRLF + 一个空格为折叠点。
    body = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:REQUEST\r\n"
        "BEGIN:VEVENT\r\nUID:fold@example.com\r\nSEQUENCE:0\r\n"
        "DTSTAMP:20260920T080000Z\r\n"
        "DTSTART:2026100\r\n 1T090000Z\r\n"
        "DTEND:20261001T100000Z\r\n"
        "SUMMARY:Folded\r\n  subject\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    post(client, mail(calendar_part(body)))
    payload = events_response(client)
    assert payload["diagnostics"] == []
    ev = payload["events"][0]
    assert ev["uid"] == "fold@example.com"
    assert ev["dtstart"] == "2026-10-01T09:00:00Z"
    assert ev["summary"] == "Folded subject"


def test_leading_fold_diagnostic(client):
    body = (
        " BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        "UID:x@example.com\r\nDTSTAMP:20260920T080000Z\r\n"
        "DTSTART:20261001T090000Z\r\nDTEND:20261001T100000Z\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    post(client, mail(calendar_part(body)))
    payload = events_response(client)
    d = find_diag(payload, "CALENDAR_MALFORMED", reason="fold_without_previous_line")
    assert d is not None
    assert d["message_id"] == 1
    assert d["node_path"] == "2"


def test_lf_and_bare_cr_newlines(client):
    body = (
        "BEGIN:VCALENDAR\nMETHOD:REQUEST\nBEGIN:VEVENT\n"
        "UID:lf@example.com\nDTSTAMP:20260920T080000Z\n"
        "DTSTART;TZID=Europe/Berlin:20261001T110000\n"
        "DTEND;TZID=Europe/Berlin:20261001T120000\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    post(client, mail(calendar_part(body)))
    payload = events_response(client)
    assert payload["diagnostics"] == []
    ev = payload["events"][0]
    assert ev["dtstart"] == "2026-10-01T09:00:00Z"
    assert ev["dtend"] == "2026-10-01T10:00:00Z"


# ---------------------------------------------------------------------------
# 时区转换与夏令时
# ---------------------------------------------------------------------------


def test_iana_tzid_winter_and_summer(client):
    # 冬季 UTC+1、夏季 UTC+2（柏林 2026 夏令时 03-29 至 10-25）。
    post(
        client,
        mail(
            calendar_part(
                ical_text(
                    vevent(
                        "winter@example.com",
                        dtstart="20260115T100000",
                        dtend="20260115T110000",
                        start_params=";TZID=Europe/Berlin",
                        end_params=";TZID=Europe/Berlin",
                    )
                )
            )
        ),
    )
    post(
        client,
        mail(
            calendar_part(
                ical_text(
                    vevent(
                        "summer@example.com",
                        dtstart="20260715T100000",
                        dtend="20260715T110000",
                        start_params=";TZID=Europe/Berlin",
                        end_params=";TZID=Europe/Berlin",
                    )
                )
            )
        ),
    )
    payload = events_response(client)
    assert payload["diagnostics"] == []
    by_uid = {e["uid"]: e for e in payload["events"]}
    assert by_uid["winter@example.com"]["dtstart"] == "2026-01-15T09:00:00Z"
    assert by_uid["summer@example.com"]["dtstart"] == "2026-07-15T08:00:00Z"


def test_america_new_york_offsets(client):
    post(
        client,
        mail(
            calendar_part(
                ical_text(
                    vevent(
                        "ny@example.com",
                        dtstart="20260115T090000",
                        dtend="20260115T100000",
                        start_params=";TZID=America/New_York",
                        end_params=";TZID=America/New_York",
                    )
                )
            )
        ),
    )
    payload = events_response(client)
    assert payload["diagnostics"] == []
    ev = payload["events"][0]
    assert ev["dtstart"] == "2026-01-15T14:00:00Z"


def test_dst_gap_nonexistent_local_time(client):
    # 2026-03-08 02:30 在纽约不存在（02:00 -> 03:00 跳时）。
    post(
        client,
        mail(
            calendar_part(
                ical_text(
                    vevent(
                        "gap@example.com",
                        dtstart="20260308T023000",
                        dtend="20260308T033000",
                        start_params=";TZID=America/New_York",
                        end_params=";TZID=America/New_York",
                    )
                )
            )
        ),
    )
    payload = events_response(client)
    d = find_diag(payload, "INVALID_EVENT_TIME", reason="dst_gap")
    assert d is not None
    assert d["detail"]["field"] == "DTSTART"
    assert d["detail"]["tzid"] == "America/New_York"
    assert payload["events"] == []


def test_dst_ambiguous_fold_local_time(client):
    # 2026-11-01 01:30 在纽约发生两次（EDT 与 EST）。
    post(
        client,
        mail(
            calendar_part(
                ical_text(
                    vevent(
                        "amb@example.com",
                        dtstart="20261101T013000",
                        dtend="20261101T023000",
                        start_params=";TZID=America/New_York",
                        end_params=";TZID=America/New_York",
                    )
                )
            )
        ),
    )
    payload = events_response(client)
    d = find_diag(payload, "INVALID_EVENT_TIME", reason="dst_ambiguous")
    assert d is not None
    assert d["detail"]["field"] == "DTSTART"
    assert payload["events"] == []


def test_floating_time_diagnosed(client):
    post(
        client,
        mail(
            calendar_part(
                ical_text(
                    vevent(
                        "float@example.com",
                        dtstart="20261001T090000",
                        dtend="20261001T100000",
                    )
                )
            )
        ),
    )
    payload = events_response(client)
    d = find_diag(payload, "INVALID_EVENT_TIME", reason="floating_time")
    assert d is not None
    assert d["detail"]["field"] == "DTSTART"
    assert payload["events"] == []


def test_date_value_diagnosed(client):
    post(
        client,
        mail(
            calendar_part(
                ical_text(
                    vevent(
                        "allday@example.com",
                        dtstart="20261001",
                        dtend="20261002",
                        start_params=";VALUE=DATE",
                        end_params=";VALUE=DATE",
                    )
                )
            )
        ),
    )
    payload = events_response(client)
    reasons = {d["detail"].get("reason") for d in payload["diagnostics"]}
    assert "date_not_datetime" in reasons
    assert payload["events"] == []


def test_unknown_tzid_diagnosed(client):
    post(
        client,
        mail(
            calendar_part(
                ical_text(
                    vevent(
                        "weird@example.com",
                        dtstart="20261001T090000",
                        dtend="20261001T100000",
                        start_params=";TZID=Mars/Olympus",
                        end_params=";TZID=Mars/Olympus",
                    )
                )
            )
        ),
    )
    payload = events_response(client)
    d = find_diag(payload, "INVALID_EVENT_TIME", reason="unknown_tzid")
    assert d is not None
    assert payload["events"] == []


def test_utc_z_with_tzid_diagnosed(client):
    post(
        client,
        mail(
            calendar_part(
                ical_text(
                    vevent(
                        "both@example.com",
                        dtstart="20261001T090000Z",
                        start_params=";TZID=Europe/Berlin",
                    )
                )
            )
        ),
    )
    payload = events_response(client)
    assert find_diag(payload, "INVALID_EVENT_TIME", reason="utc_with_tzid")
    assert payload["events"] == []


# ---------------------------------------------------------------------------
# 必填字段与字段校验
# ---------------------------------------------------------------------------


def test_missing_required_fields_listed(client):
    body = (
        "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\n"
        "SUMMARY:no identity\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    post(client, mail(calendar_part(body)))
    payload = events_response(client)
    d = find_diag(payload, "MISSING_EVENT_FIELD")
    assert d is not None
    assert d["detail"]["fields"] == ["UID", "DTSTAMP", "DTSTART", "DTEND"]
    assert d["message_id"] == 1 and d["node_path"] == "2"
    assert payload["events"] == []


def test_invalid_sequence_diagnosed(client):
    post(client, mail(calendar_part(ical_text(vevent(sequence="abc")))))
    payload = events_response(client)
    d = find_diag(payload, "INVALID_EVENT_FIELD", reason="sequence_not_integer")
    assert d is not None
    assert d["detail"]["value"] == "abc"
    assert payload["events"] == []


def test_negative_sequence_diagnosed(client):
    post(client, mail(calendar_part(ical_text(vevent(sequence="-2")))))
    payload = events_response(client)
    assert find_diag(payload, "INVALID_EVENT_FIELD", reason="sequence_not_integer")
    assert payload["events"] == []


def test_garbage_timestamp_diagnosed_per_field(client):
    post(
        client,
        mail(
            calendar_part(
                ical_text(
                    vevent(
                        dtstart="not-a-time",
                        dtend="also-bad",
                    )
                )
            )
        ),
    )
    payload = events_response(client)
    bad = [
        d for d in payload["diagnostics"]
        if d["code"] == "INVALID_EVENT_TIME" and d["detail"]["reason"] == "not_iso"
    ]
    assert {d["detail"]["field"] for d in bad} == {"DTSTART", "DTEND"}
    assert payload["events"] == []


def test_impossible_calendar_date_diagnosed(client):
    post(
        client,
        mail(calendar_part(ical_text(vevent(dtstart="20260230T090000Z"))))
    )
    payload = events_response(client)
    d = find_diag(payload, "INVALID_EVENT_TIME")
    assert d is not None
    assert d["detail"]["field"] == "DTSTART"
    assert d["detail"]["value"] == "20260230T090000Z"
    assert payload["events"] == []


def test_summary_text_unescaped(client):
    post(
        client,
        mail(
            calendar_part(
                ical_text(
                    vevent(summary=r"Line1\nLine2\, with semicolon\; and \\backslash")
                )
            )
        ),
    )
    payload = events_response(client)
    assert payload["diagnostics"] == []
    assert (
        payload["events"][0]["summary"]
        == "Line1\nLine2, with semicolon; and \\backslash"
    )


def test_duplicate_single_valued_property_invalidates_event(client):
    body = (
        "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\n"
        "UID:dup-prop@example.com\r\nDTSTAMP:20260920T080000Z\r\n"
        "DTSTART:20261001T090000Z\r\nDTEND:20261001T100000Z\r\n"
        "DTSTART:20261002T090000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    post(client, mail(calendar_part(body)))
    payload = events_response(client)
    d = find_diag(payload, "INVALID_EVENT_FIELD", reason="duplicate_property")
    assert d is not None
    assert d["detail"]["property"] == "DTSTART"
    assert payload["events"] == []


def test_malformed_line_diagnosed_but_other_event_survives(client):
    body = (
        "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\n"
        "GARBAGE_NO_COLON_LINE\r\n"
        + vevent("good@example.com")
        + "\r\nEND:VCALENDAR\r\n"
    )
    post(client, mail(calendar_part(body)))
    payload = events_response(client)
    assert find_diag(payload, "CALENDAR_INVALID_LINE")
    assert [e["uid"] for e in payload["events"]] == ["good@example.com"]


def test_no_vcalendar_wrapper_diagnosed(client):
    body = (
        "BEGIN:VEVENT\r\nUID:lone@example.com\r\n"
        "DTSTAMP:20260920T080000Z\r\n"
        "DTSTART:20261001T090000Z\r\nDTEND:20261001T100000Z\r\n"
        "END:VEVENT\r\n"
    )
    post(client, mail(calendar_part(body)))
    payload = events_response(client)
    assert find_diag(payload, "CALENDAR_MALFORMED", reason="no_vcalendar")
    assert [e["uid"] for e in payload["events"]] == ["lone@example.com"]


def test_unclosed_component_diagnosed(client):
    body = (
        "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\n"
        "UID:open@example.com\r\nDTSTAMP:20260920T080000Z\r\n"
        "DTSTART:20261001T090000Z\r\nDTEND:20261001T100000Z\r\n"
    )
    post(client, mail(calendar_part(body)))
    payload = events_response(client)
    assert find_diag(payload, "CALENDAR_MALFORMED", reason="unclosed_component")
    assert payload["events"] == []


# ---------------------------------------------------------------------------
# 同节点重复 UID
# ---------------------------------------------------------------------------


def test_duplicate_uid_within_same_node_each_diagnosed(client):
    body = ical_text(
        [
            vevent("same@example.com", sequence=1, dtstamp="20260920T080000Z"),
            vevent("same@example.com", sequence=2, dtstamp="20260921T080000Z"),
        ]
    )
    post(client, mail(calendar_part(body)))
    payload = events_response(client)
    dups = [
        d for d in payload["diagnostics"] if d["code"] == "DUPLICATE_UID_IN_NODE"
    ]
    assert len(dups) == 2
    assert {d["detail"]["event_index"] for d in dups} == {0, 1}
    for d in dups:
        assert d["detail"]["uid"] == "same@example.com"
        assert d["detail"]["occurrences"] == 2
        assert d["message_id"] == 1 and d["node_path"] == "2"
    assert payload["events"] == []


def test_same_uid_in_different_nodes_is_not_duplicate_diagnostic(client):
    post(client, mail(calendar_part(ical_text(vevent("cross@example.com")))))
    post(client, mail(calendar_part(ical_text(vevent("cross@example.com", sequence=1)))))
    payload = events_response(client)
    assert not find_diag(payload, "DUPLICATE_UID_IN_NODE")
    assert len(payload["events"]) == 1
    assert payload["events"][0]["sequence"] == 1
    assert payload["events"][0]["message_id"] == 2


# ---------------------------------------------------------------------------
# 跨邮件版本选择
# ---------------------------------------------------------------------------


def test_version_prefers_higher_sequence(client):
    post(client, mail(calendar_part(ical_text(vevent("v@example.com", sequence=3, dtstamp="20260925T080000Z")))))
    post(client, mail(calendar_part(ical_text(vevent("v@example.com", sequence=5, dtstamp="20260920T080000Z")))))
    post(client, mail(calendar_part(ical_text(vevent("v@example.com", sequence=4, dtstamp="20260928T080000Z")))))
    payload = events_response(client)
    assert len(payload["events"]) == 1
    ev = payload["events"][0]
    assert ev["sequence"] == 5
    assert ev["message_id"] == 2


def test_version_tie_sequence_uses_later_dtstamp(client):
    post(client, mail(calendar_part(ical_text(vevent("v@example.com", sequence=1, dtstamp="20260920T080000Z")))))
    post(client, mail(calendar_part(ical_text(vevent("v@example.com", sequence=1, dtstamp="20260922T080000Z")))))
    payload = events_response(client)
    ev = payload["events"][0]
    assert ev["dtstamp"] == "2026-09-22T08:00:00Z"
    assert ev["message_id"] == 2


def test_version_full_tie_uses_message_id(client):
    same = ical_text(vevent("v@example.com", sequence=1, dtstamp="20260920T080000Z"))
    post(client, mail(calendar_part(same), subject="one"))
    post(client, mail(calendar_part(same), subject="two"))
    payload = events_response(client)
    assert len(payload["events"]) == 1
    assert payload["events"][0]["message_id"] == 2


def test_cancel_marks_cancelled_and_keeps_source(client):
    post(
        client,
        mail(calendar_part(ical_text(vevent("meet@example.com", sequence=2, dtstamp="20260920T080000Z"), method="REQUEST"))),
    )
    post(
        client,
        mail(calendar_part(ical_text(vevent("meet@example.com", sequence=3, dtstamp="20260921T080000Z"), method="CANCEL"))),
    )
    payload = events_response(client)
    assert len(payload["events"]) == 1
    ev = payload["events"][0]
    assert ev["cancelled"] is True
    assert ev["method"] == "CANCEL"
    assert ev["sequence"] == 3
    assert ev["message_id"] == 2
    assert ev["node_path"] == "2"
    # 取消事件仍保留完整字段与来源。
    assert ev["dtstart"] == "2026-10-01T09:00:00Z"
    assert ev["uid"] == "meet@example.com"


def test_cancel_with_lower_sequence_loses(client):
    post(
        client,
        mail(calendar_part(ical_text(vevent("meet@example.com", sequence=5), method="CANCEL"))),
    )
    post(
        client,
        mail(calendar_part(ical_text(vevent("meet@example.com", sequence=6), method="REQUEST"))),
    )
    payload = events_response(client)
    ev = payload["events"][0]
    assert ev["sequence"] == 6
    assert ev["cancelled"] is False


def test_invalid_event_does_not_participate_in_selection(client):
    # 第一封：无效（浮动时间）；第二封：有效低 SEQUENCE。
    post(
        client,
        mail(
            calendar_part(
                ical_text(
                    vevent(
                        "v@example.com",
                        sequence=99,
                        dtstart="20261001T090000",
                        dtend="20261001T100000",
                    )
                )
            )
        ),
    )
    post(client, mail(calendar_part(ical_text(vevent("v@example.com", sequence=0)))))
    payload = events_response(client)
    assert find_diag(payload, "INVALID_EVENT_TIME", reason="floating_time")
    assert len(payload["events"]) == 1
    assert payload["events"][0]["sequence"] == 0
    assert payload["events"][0]["message_id"] == 2


# ---------------------------------------------------------------------------
# 排序、结构与只读
# ---------------------------------------------------------------------------


def test_sorted_by_start_then_uid(client):
    post(
        client,
        mail(
            calendar_part(
                ical_text(
                    [
                        vevent(
                            "later@example.com",
                            dtstart="20261005T090000Z",
                            dtend="20261005T100000Z",
                        ),
                        vevent(
                            "earlier@example.com",
                            dtstart="20261001T090000Z",
                            dtend="20261001T100000Z",
                        ),
                    ]
                )
            )
        ),
    )
    payload = events_response(client)
    assert [e["uid"] for e in payload["events"]] == [
        "earlier@example.com",
        "later@example.com",
    ]


def test_same_start_sorted_by_uid(client):
    post(
        client,
        mail(
            calendar_part(
                ical_text(
                    [
                        vevent("zeta@example.com"),
                        vevent("alpha@example.com"),
                    ]
                )
            )
        ),
    )
    payload = events_response(client)
    assert [e["uid"] for e in payload["events"]] == [
        "alpha@example.com",
        "zeta@example.com",
    ]


def test_calendar_inside_message_rfc822_attachment(client):
    inner = (
        b"Subject: nested calendar\r\n"
        + b"Content-Type: text/calendar; charset=utf-8\r\n\r\n"
        + ical_text(vevent("nested@example.com")).encode()
    )
    wrapper = (
        b"Content-Type: message/rfc822\r\n\r\n" + inner
    )
    post(client, mail(wrapper))
    payload = events_response(client)
    assert payload["diagnostics"] == []
    assert len(payload["events"]) == 1
    ev = payload["events"][0]
    assert ev["uid"] == "nested@example.com"
    assert ev["node_path"] == "2/1"


def test_multiple_calendar_nodes_in_one_message(client):
    post(
        client,
        mail(
            calendar_part(ical_text(vevent("a@example.com"))),
            calendar_part(ical_text(vevent("b@example.com"))),
        ),
    )
    payload = events_response(client)
    assert {e["uid"] for e in payload["events"]} == {
        "a@example.com",
        "b@example.com",
    }
    assert {e["node_path"] for e in payload["events"]} == {"2", "3"}


def test_base64_content_transfer_encoding(client):
    post(client, mail(calendar_part(ical_text(vevent("b64@example.com")), cte="base64")))
    payload = events_response(client)
    assert payload["diagnostics"] == []
    assert payload["events"][0]["uid"] == "b64@example.com"


def test_non_calendar_text_ignored(client):
    post(
        client,
        mail(
            text_part("BEGIN:VCALENDAR but it is just a text part"),
            calendar_part(ical_text(vevent("real@example.com"))),
        ),
    )
    payload = events_response(client)
    assert [e["uid"] for e in payload["events"]] == ["real@example.com"]


def test_read_only_does_not_mutate_archive(client):
    msg_id = post(client, mail(calendar_part(ical_text(vevent()))))
    before = client.get(f"/messages/{msg_id}/nodes/2").json()
    client.get("/calendar/events")
    client.get("/calendar/events")
    after = client.get(f"/messages/{msg_id}/nodes/2").json()
    assert before == after


def test_diagnostics_sorted_stably(client):
    # 两封邮件各带一个坏节点 + 一个重复 UID 节点。
    bad = ical_text(vevent("x1@example.com", dtstart="bad", dtend="bad"))
    post(client, mail(calendar_part(bad)))
    dup = ical_text([vevent("d@example.com"), vevent("d@example.com")])
    post(client, mail(calendar_part(dup)))
    payload = events_response(client)
    keys = [(d["message_id"], d["node_path"], d["code"]) for d in payload["diagnostics"]]
    assert keys == sorted(keys)


def test_vtimezone_tzid_matches_iana_definition(client):
    # 即使节点自带 VTIMEZONE 定义，转换以 IANA 数据库为准；VALARM 内
    # 的 UID 等属性不得污染事件。
    body = (
        "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\n"
        "BEGIN:VTIMEZONE\r\nTZID:Europe/Berlin\r\n"
        "BEGIN:STANDARD\r\nDTSTART:19701025T030000\r\n"
        "TZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100\r\nEND:STANDARD\r\n"
        "END:VTIMEZONE\r\n"
        "BEGIN:VEVENT\r\nUID:tz@example.com\r\nSEQUENCE:0\r\n"
        "DTSTAMP:20260920T080000Z\r\n"
        "DTSTART;TZID=Europe/Berlin:20260115T100000\r\n"
        "DTEND;TZID=Europe/Berlin:20260115T110000\r\n"
        "SUMMARY:With zone def\r\n"
        "BEGIN:VALARM\r\nUID:alarm-uid-should-not-leak\r\n"
        "ACTION:DISPLAY\r\nEND:VALARM\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    post(client, mail(calendar_part(body)))
    payload = events_response(client)
    assert payload["diagnostics"] == []
    ev = payload["events"][0]
    assert ev["uid"] == "tz@example.com"
    assert ev["dtstart"] == "2026-01-15T09:00:00Z"


def test_multiple_vcalendars_in_one_node_take_own_method(client):
    body = (
        "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\n"
        + vevent("req@example.com")
        + "\r\nEND:VCALENDAR\r\n"
        "BEGIN:VCALENDAR\r\nMETHOD:CANCEL\r\n"
        + vevent("can@example.com")
        + "\r\nEND:VCALENDAR\r\n"
    )
    post(client, mail(calendar_part(body)))
    payload = events_response(client)
    assert payload["diagnostics"] == []
    by_uid = {e["uid"]: e for e in payload["events"]}
    assert by_uid["req@example.com"]["cancelled"] is False
    assert by_uid["can@example.com"]["cancelled"] is True


def test_quoted_tzid_parameter(client):
    # 引号包裹的参数值按 RFC 5545 参数语法合法解析。
    post(
        client,
        mail(
            calendar_part(
                ical_text(
                    vevent(
                        "q@example.com",
                        dtstart="20260115T100000",
                        dtend="20260115T110000",
                        start_params=';TZID="Europe/Berlin"',
                        end_params=';TZID="Europe/Berlin"',
                    )
                )
            )
        ),
    )
    payload = events_response(client)
    assert payload["diagnostics"] == []
    assert payload["events"][0]["dtstart"] == "2026-01-15T09:00:00Z"


def test_empty_calendar_body_diagnosed(client):
    post(client, mail(calendar_part("")))
    payload = events_response(client)
    assert find_diag(payload, "CALENDAR_MALFORMED", reason="empty_body")
    assert payload["events"] == []


def test_dtend_can_use_utc_while_start_uses_tzid(client):
    post(
        client,
        mail(
            calendar_part(
                ical_text(
                    vevent(
                        "mixed@example.com",
                        dtstart="20260115T110000",
                        dtend="20260115T100000Z",
                        start_params=";TZID=Europe/Berlin",
                    )
                )
            )
        ),
    )
    payload = events_response(client)
    assert payload["diagnostics"] == []
    ev = payload["events"][0]
    # 柏林冬季 11:00 = UTC 10:00；DTEND 直接以 Z 给出同一时刻。
    assert ev["dtstart"] == ev["dtend"] == "2026-01-15T10:00:00Z"


def test_one_bad_event_among_good_ones(client):
    body = ical_text(
        [
            vevent("good1@example.com"),
            vevent("bad@example.com", dtstart="20261001T090000", dtend="20261001T100000"),
            vevent("good2@example.com", dtstamp="20260920T080000Z"),
        ]
    )
    post(client, mail(calendar_part(body)))
    payload = events_response(client)
    assert find_diag(payload, "INVALID_EVENT_TIME", reason="floating_time")
    assert sorted(e["uid"] for e in payload["events"]) == [
        "good1@example.com",
        "good2@example.com",
    ]


def test_root_level_text_calendar(client):
    # 整封邮件本身就是 text/calendar（根节点为文本叶子，path 为空）。
    data = (
        b"Content-Type: text/calendar; charset=utf-8\r\n\r\n"
        + ical_text(vevent("root@example.com")).rstrip("\r\n").encode()
    )
    post(client, data)
    payload = events_response(client)
    assert payload["diagnostics"] == []
    assert len(payload["events"]) == 1
    assert payload["events"][0]["node_path"] == ""


def test_diagnostic_message_and_node_locators(client):
    mid = post(
        client,
        mail(
            calendar_part(
                ical_text(
                    vevent("x@example.com", dtstart="20261001T090000", dtend="20261001T100000")
                )
            )
        ),
    )
    payload = events_response(client)
    assert len(payload["diagnostics"]) >= 1
    for d in payload["diagnostics"]:
        assert d["message_id"] == mid
        assert isinstance(d["node_path"], str)


def test_nested_vevent_diagnosed_and_discarded(client):
    body = (
        "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\n"
        "BEGIN:VEVENT\r\nUID:outer@example.com\r\n"
        "DTSTAMP:20260920T080000Z\r\n"
        "DTSTART:20261001T090000Z\r\nDTEND:20261001T100000Z\r\n"
        "BEGIN:VEVENT\r\nUID:inner@example.com\r\nEND:VEVENT\r\n"
        "SUMMARY:outer stays invalid\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    post(client, mail(calendar_part(body)))
    payload = events_response(client)
    assert find_diag(payload, "CALENDAR_MALFORMED", reason="nested_vevent")
    assert payload["events"] == []


def test_end_without_begin_diagnosed_but_stream_recovers(client):
    body = (
        "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\n"
        "END:VTODO\r\n"
        + vevent("ok@example.com")
        + "\r\nEND:VCALENDAR\r\n"
    )
    post(client, mail(calendar_part(body)))
    payload = events_response(client)
    assert find_diag(
        payload,
        "CALENDAR_MALFORMED",
        reason="end_without_matching_begin",
    )
    assert [e["uid"] for e in payload["events"]] == ["ok@example.com"]


def test_method_inside_vtimezone_ignored(client):
    # VTIMEZONE 里的 METHOD 不是日历方法；事件 method 必须为 None。
    body = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VTIMEZONE\r\nTZID:Europe/Berlin\r\n"
        "METHOD:CANCEL\r\nEND:VTIMEZONE\r\n"
        + vevent("nomethod@example.com")
        + "\r\nEND:VCALENDAR\r\n"
    )
    post(client, mail(calendar_part(body)))
    payload = events_response(client)
    assert payload["diagnostics"] == []
    ev = payload["events"][0]
    assert ev["method"] is None
    assert ev["cancelled"] is False


def test_sequence_large_value(client):
    post(client, mail(calendar_part(ical_text(vevent(sequence=4294967295)))))
    payload = events_response(client)
    assert payload["events"][0]["sequence"] == 4294967295

