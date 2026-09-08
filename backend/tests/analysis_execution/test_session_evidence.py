"""Synthetic ordinary CLI streams, with no model calls."""

import json

import pytest

from app.analysis_execution.session_evidence import verify_session_output

SESSION = "ed794e42-3e98-4408-85e6-5e6ae75d8499"


def events():
    return [
        {
            "type": "system",
            "subtype": "init",
            "session_id": SESSION,
            "model": "m",
            "claude_code_version": "2.1.261",
            "cwd": "D:/work",
        },
        {
            "type": "assistant",
            "session_id": SESSION,
            "message": {"content": [{"type": "text", "text": "untrusted narration"}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "session_id": SESSION,
            "is_error": False,
            "result": '{"accepted":true}',
        },
    ]


def verify(stream, **kwargs):
    raw = b"\n".join(json.dumps(event).encode() for event in stream)
    return verify_raw(raw, **kwargs)


def verify_raw(raw, **kwargs):
    args = {
        "session_id": SESSION,
        "expected_model": "m",
        "cli_version": "2.1.261",
        "cwd": "D:/work",
    }
    return verify_session_output(raw, **(args | kwargs))


def test_create_and_resume_use_the_same_ordinary_format():
    assert verify(events()) == {"accepted": True}
    assert verify(events()) == {"accepted": True}


@pytest.mark.parametrize(
    "index,key,value",
    [
        (0, "session_id", "wrong"),
        (1, "session_id", "wrong"),
        (2, "session_id", "wrong"),
        (0, "model", "wrong"),
        (0, "cwd", "D:/elsewhere"),
        (0, "claude_code_version", "2.1.262"),
        (2, "is_error", True),
        (2, "subtype", "error"),
        (1, "error", "failed"),
    ],
)
def test_mismatch_and_error_are_rejected(index, key, value):
    stream = events()
    stream[index][key] = value
    with pytest.raises(ValueError):
        verify(stream)


@pytest.mark.parametrize(
    "value",
    ["not json", "```json\n{}\n```", "[]", "{} prose", '{"a":1,"a":2}', '{"x":NaN}', None, {}],
)
def test_only_strict_terminal_json_object_is_accepted(value):
    stream = events()
    stream[1]["message"]["content"][0]["text"] = '{"accepted":true}'
    stream[-1]["result"] = value
    with pytest.raises(ValueError):
        verify(stream)


@pytest.mark.parametrize(
    "mode",
    [
        "duplicate_init",
        "duplicate_result",
        "missing_init",
        "missing_result",
        "before_init",
        "after_result",
        "child",
        "task_started",
        "task_notification",
        "stream_tool",
        "error",
    ],
)
def test_event_order_and_nested_execution_are_rejected(mode):
    stream = events()
    if mode == "duplicate_init":
        stream.insert(1, dict(stream[0]))
    elif mode == "duplicate_result":
        stream.append(dict(stream[-1]))
    elif mode == "missing_init":
        stream.pop(0)
    elif mode == "missing_result":
        stream.pop()
    elif mode == "before_init":
        stream[0], stream[1] = stream[1], stream[0]
    elif mode == "after_result":
        stream.append(dict(stream[1]))
    elif mode == "child":
        stream[1]["parent_tool_use_id"] = "child"
    elif mode == "stream_tool":
        stream.insert(
            1,
            {
                "type": "stream_event",
                "session_id": SESSION,
                "event": {
                    "type": "content_block_start",
                    "content_block": {"type": "tool_use", "name": "Agent"},
                },
            },
        )
    else:
        stream.insert(1, {"type": "system", "subtype": mode, "session_id": SESSION})
    with pytest.raises(ValueError):
        verify(stream)


@pytest.mark.parametrize("name", ["Agent", "Task", "SendMessage", "Bash", "Write"])
def test_only_read_tool_is_allowed(name):
    stream = events()
    stream[1]["message"]["content"] = [{"type": "tool_use", "name": name, "id": "r"}]
    with pytest.raises(ValueError):
        verify(stream)


def test_read_tool_and_successful_result_are_allowed():
    stream = events()
    stream[1]["message"]["content"] = [{"type": "tool_use", "name": "Read", "id": "r"}]
    stream.insert(
        2,
        {
            "type": "user",
            "session_id": SESSION,
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "r", "content": "file text"}]
            },
        },
    )
    assert verify(stream) == {"accepted": True}
    stream[2]["message"]["content"][0]["is_error"] = True
    with pytest.raises(ValueError):
        verify(stream)


@pytest.mark.parametrize("raw", [b"{", b"[]", b"\xff", b'{"type":"x","type":"y"}'])
def test_malformed_stream_is_rejected(raw):
    with pytest.raises(ValueError):
        verify_raw(raw)


def test_unsupported_adapter_and_non_uuid_are_rejected():
    with pytest.raises(ValueError):
        verify(events(), cli_version="2.1.262")
    stream = events()
    for event in stream:
        event["session_id"] = "s"
    with pytest.raises(ValueError):
        verify(stream, session_id="s")


@pytest.mark.parametrize(
    "extra",
    [
        {"type": "system", "subtype": "task_progress"},
        {"type": "system", "subtype": "task_failed"},
        {"type": "system", "subtype": "status", "task_id": "child"},
        {"type": "assistant", "agent_id": "child"},
        {"type": "assistant", "spawn_depth": 1},
        {"type": "assistant", "parent_agent_id": "parent"},
    ],
)
def test_all_task_events_and_explicit_child_metadata_are_rejected(extra):
    stream = events()
    stream.insert(1, {"session_id": SESSION} | extra)
    with pytest.raises(ValueError):
        verify(stream)


def test_business_json_strings_are_not_interpreted_as_child_events():
    stream = events()
    stream[-1]["result"] = '{"task_id":"business","agent_id":"business","spawn_depth":1}'
    assert verify(stream) == {"task_id": "business", "agent_id": "business", "spawn_depth": 1}
