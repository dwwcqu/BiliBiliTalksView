"""Synthetic Claude stream boundaries; no model or network calls."""

import json

import pytest

from app.analysis_execution.events import parse_events

SESSION = "b145514e-026d-4fcf-823b-c5655588a8bb"
INIT = {"type": "system", "subtype": "init", "session_id": SESSION}
RESULT = {
    "type": "result", "subtype": "success", "is_error": False,
    "session_id": SESSION, "result": "synthetic", "total_cost_usd": 0.125,
}


def parse(*events, returncode=0):
    raw = b"\n".join(json.dumps(event).encode() for event in events)
    return parse_events(raw, session_id=SESSION, returncode=returncode)


def assert_failed(value):
    assert value["status"] == "failed"
    assert isinstance(value["error_code"], str)
    assert value["estimated_cost_usd"] is None
    assert value["cost_status"] == "unknown"


def test_success_keeps_structured_output_and_exact_cost():
    value = parse(INIT, {**RESULT, "structured_output": {"ok": True}})
    assert value["status"] == "transport_succeeded"
    assert value["error_code"] is None
    assert value["result"]["structured_output"] == {"ok": True}
    assert value["estimated_cost_usd"] == "0.125"
    assert value["cost_status"] == "estimated"


def test_last_main_result_selected_without_summing_costs():
    value = parse(INIT, RESULT, {**RESULT, "result": "last"})
    assert value["status"] == "transport_succeeded"
    assert value["result"]["result"] == "last"
    assert value["estimated_cost_usd"] is None
    assert value["cost_status"] == "unknown"


def test_child_result_does_not_supply_main_result_or_poison_main_failure():
    child = {**RESULT, "session_id": "child", "parent_tool_use_id": "tool-1",
             "is_error": True, "subtype": "error_during_execution"}
    assert_failed(parse(INIT, child))
    assert parse(INIT, child, RESULT)["status"] == "transport_succeeded"


@pytest.mark.parametrize("event", [
    {**RESULT, "is_error": True},
    {**RESULT, "subtype": "error_max_turns"},
    {**RESULT, "is_error": "false"},
    {**RESULT, "subtype": None},
    {**RESULT, "session_id": "other"},
    {**RESULT, "session_id": None},
    {**RESULT, "parent_tool_use_id": []},
    {"type": "assistant", "session_id": "other"},
    {**INIT, "session_id": "other"},
])
def test_invalid_main_event_cannot_be_hidden_by_later_success(event):
    assert_failed(parse(INIT, event, RESULT))


@pytest.mark.parametrize("events", [[], [RESULT], [INIT], [RESULT, INIT]])
def test_requires_init_before_terminal_main_result(events):
    assert_failed(parse(*events))


def test_nonzero_exit_cannot_report_success():
    assert_failed(parse(INIT, RESULT, returncode=2))


@pytest.mark.parametrize("tail", [b"not-json", b"[]", b"null", b"\xff",
    b'{"type":"note","type":"other"}', b'{"cost":NaN}'])
def test_bad_trailing_stream_cannot_be_hidden_by_result(tail):
    raw = json.dumps(INIT).encode() + b"\n" + json.dumps(RESULT).encode() + b"\n" + tail
    assert_failed(parse_events(raw, session_id=SESSION, returncode=0))


def test_unknown_notifications_after_result_allowed():
    assert parse(INIT, RESULT, {"type": "future_notification"})["status"] == (
        "transport_succeeded"
    )


@pytest.mark.parametrize("fee", [None, -1, True, False, "0.1", {}, [], "Infinity"])
def test_invalid_fee_is_unknown_without_changing_transport_success(fee):
    value = parse(INIT, {**RESULT, "total_cost_usd": fee})
    assert value["status"] == "transport_succeeded"
    assert value["cost_status"] == "unknown"
    assert value["estimated_cost_usd"] is None


def test_missing_fee_is_unknown_and_explicit_zero_is_estimated():
    event = {key: value for key, value in RESULT.items() if key != "total_cost_usd"}
    assert parse(INIT, event)["cost_status"] == "unknown"
    assert parse(INIT, {**RESULT, "total_cost_usd": 0})["estimated_cost_usd"] == "0"


def test_unicode_separators_inside_comment_text_preserved():
    comment = "first\u2028second\u0085third\u2029last"
    event = {**RESULT, "result": comment}
    raw = (json.dumps(INIT) + "\n" + json.dumps(event, ensure_ascii=False)).encode("utf-8")
    value = parse_events(raw, session_id=SESSION, returncode=0)
    assert value["status"] == "transport_succeeded"
    assert value["result"]["result"] == comment
