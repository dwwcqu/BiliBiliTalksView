"""Synthetic creation evidence: no CLI or model calls."""

import hashlib
import json

import pytest

from app.analysis_execution.creation_evidence import verify_creation


def events():
    return [
        {
            "type": "system",
            "subtype": "init",
            "session_id": "s",
            "model": "m",
            "claude_code_version": "2.1.261",
            "cwd": "D:/work",
        },
        {
            "type": "assistant",
            "session_id": "s",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call",
                        "name": "Agent",
                        "input": {
                            "description": "initialize",
                            "prompt": "exact\u2028message",
                            "subagent_type": "member",
                            "run_in_background": False,
                        },
                    }
                ]
            },
        },
        {
            "type": "system",
            "subtype": "task_started",
            "session_id": "s",
            "task_id": "native-id",
            "tool_use_id": "call",
            "subagent_type": "member",
            "spawn_depth": 1,
            "task_type": "local_agent",
        },
        {
            "type": "system",
            "subtype": "task_notification",
            "session_id": "s",
            "task_id": "native-id",
            "tool_use_id": "call",
            "status": "completed",
            "summary": json.dumps(
                {"initialization_sha256": "digest", "member_id": "member", "role": "role"}
            ),
        },
        {
            "type": "user",
            "session_id": "s",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call",
                        "is_error": False,
                        "content": [
                            {"type": "text", "text": "output\nagentId: fabricated-text-id"}
                        ],
                    }
                ]
            },
        },
        {"type": "result", "subtype": "success", "session_id": "s", "is_error": False},
    ]


def verify(stream, message="exact\u2028message", **options):
    raw = b"\n".join(json.dumps(event, ensure_ascii=False).encode() for event in stream)
    return verify_creation(
        raw,
        session_id="s",
        member_id="member",
        role="role",
        native_type="member",
        message=message,
        **options,
        initialization_sha256="digest",
        expected_model="m",
        cli_version="2.1.261",
        cwd="D:/work",
    ), raw


def test_native_identity_and_late_tool_result():
    proof, raw = verify(events())
    assert proof == {
        "agent_id": "native-id",
        "parent_budget_stopped": False,
        "acknowledgement_repaired": False,
        "creation_tool_use_id": "call",
        "stdout_sha256": hashlib.sha256(raw).hexdigest(),
    }


def test_early_tool_result_and_intermediate_results():
    stream = events()
    stream[3], stream[4] = stream[4], stream[3]
    stream.insert(3, stream[-1].copy())
    stream.append(stream[-1].copy())
    verify(stream)


@pytest.mark.parametrize("missing", range(6))
def test_required_steps(missing):
    stream = events()
    del stream[missing]
    with pytest.raises(ValueError):
        verify(stream)


@pytest.mark.parametrize(
    "index,key,value",
    [
        (0, "model", "wrong"),
        (0, "claude_code_version", "2.1.262"),
        (0, "cwd", "D:/wrong"),
        (0, "session_id", "wrong"),
        (2, "task_id", "../unsafe"),
        (2, "task_id", "a" * 129),
        (2, "tool_use_id", "wrong"),
        (2, "subagent_type", "wrong"),
        (2, "spawn_depth", True),
        (2, "task_type", "remote"),
        (3, "task_id", "wrong"),
        (3, "tool_use_id", "wrong"),
        (3, "session_id", "wrong"),
        (3, "status", "failed"),
        (3, "summary", "```json\n{}\n```"),
        (3, "summary", "{}"),
        (3, "summary", '{"initialization_sha256":"old","member_id":"member","role":"role"}'),
        (3, "summary", '{"initialization_sha256":"digest","member_id":"other","role":"role"}'),
        (3, "summary", '{"initialization_sha256":"digest","member_id":"member","role":"other"}'),
    ],
)
def test_mismatch(index, key, value):
    stream = events()
    stream[index][key] = value
    with pytest.raises(ValueError):
        verify(stream)


@pytest.mark.parametrize("index", range(5))
def test_duplicate_steps(index):
    stream = events()
    stream.insert(index, stream[index])
    with pytest.raises(ValueError):
        verify(stream)


@pytest.mark.parametrize(
    "key,value",
    [
        ("prompt", "wrong"),
        ("subagent_type", "wrong"),
        ("run_in_background", True),
        ("resume", "old"),
    ],
)
def test_wrong_agent_input(key, value):
    stream = events()
    stream[1]["message"]["content"][0]["input"][key] = value
    with pytest.raises(ValueError):
        verify(stream)


@pytest.mark.parametrize("name", ["Agent", "Task", "SendMessage"])
@pytest.mark.parametrize("child", [False, True])
def test_extra_dispatch(name, child):
    stream = events()
    extra = {
        "type": "assistant",
        "session_id": "s",
        "message": {"content": [{"type": "tool_use", "id": "extra", "name": name, "input": {}}]},
    }
    if child:
        extra.update(parent_tool_use_id="call", session_id="child")
    stream.insert(3, extra)
    with pytest.raises(ValueError):
        verify(stream)


def test_failed_result():
    stream = events()
    stream[4]["message"]["content"][0]["is_error"] = True
    with pytest.raises(ValueError):
        verify(stream)


def test_final_result_must_follow_both_evidence_steps():
    stream = events()
    stream[3], stream[5] = stream[5], stream[3]
    with pytest.raises(ValueError):
        verify(stream)


@pytest.mark.parametrize(
    "raw_suffix",
    [b"\n{", b"\n[]", b'\n{"type":"result","session_id":"s","subtype":"error","is_error":true}'],
)
def test_entire_stream_validated(raw_suffix):
    _, raw = verify(events())
    with pytest.raises(ValueError):
        verify_creation(
            raw + raw_suffix,
            session_id="s",
            member_id="member",
            role="role",
            native_type="member",
            message="exact\u2028message",
            initialization_sha256="digest",
            expected_model="m",
            cli_version="2.1.261",
            cwd="D:/work",
        )


@pytest.mark.parametrize(
    "summary",
    [
        '{"initialization_sha256":"digest","member_id":"member","role":"role","role":"role"}',
        '{"initialization_sha256":"digest","member_id":"member","role":"role","extra":true}',
    ],
)
def test_strict_handshake(summary):
    stream = events()
    stream[3]["summary"] = summary
    with pytest.raises(ValueError):
        verify(stream)


@pytest.mark.parametrize("tool_id", ["", "../unsafe", "a" * 129])
def test_unsafe_tool_identity(tool_id):
    stream = events()
    stream[1]["message"]["content"][0]["id"] = tool_id
    with pytest.raises(ValueError):
        verify(stream)


@pytest.mark.parametrize("model", ["opus", "m", "inherit", "", None])
def test_agent_cannot_override_definition_model(model):
    stream = events()
    stream[1]["message"]["content"][0]["input"]["model"] = model
    with pytest.raises(ValueError, match="creation_model_override"):
        verify(stream)


JSON_MESSAGE = '{"comments":["exact  comment\\ntext","second"],"instructions":"read"}\n'


def test_json_message_formatting_preserves_all_fields():
    stream = events()
    stream[1]["message"]["content"][0]["input"]["prompt"] = json.dumps(
        {"instructions": "read", "comments": ["exact  comment\ntext", "second"]}, indent=2
    )
    verify(stream, message=JSON_MESSAGE)


@pytest.mark.parametrize(
    "observed",
    [
        '{"comments":["exact comment\\ntext","second"],"instructions":"read"}',
        '{"comments":["second","exact  comment\\ntext"],"instructions":"read"}',
        '{"comments":["exact  comment\\ntext","second"],"instructions":"read","extra":"do more"}',
        '{"comments":["exact  comment\\ntext","second"],"instructions":"read","instructions":"read"}',
        '{"comments":["exact  comment\\ntext","second"],"instructions":"read"} extra instructions',
    ],
)
def test_json_message_rejects_changed_content(observed):
    stream = events()
    stream[1]["message"]["content"][0]["input"]["prompt"] = observed
    with pytest.raises(ValueError):
        verify(stream, message=JSON_MESSAGE)


def repaired_events():
    stream = events()
    correction = {
        "type": "assistant",
        "session_id": "s",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "SendMessage",
                    "id": "repair",
                    "input": {
                        "to": "native-id",
                        "message": "Return the original initialization acknowledgement.",
                    },
                }
            ]
        },
    }
    started = {**stream[2], "tool_use_id": "repair"}
    completed = {**stream[3], "tool_use_id": "repair"}
    resumed = {
        "type": "user",
        "session_id": "s",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "repair",
                    "is_error": False,
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"success": True, "resumedAgentId": "native-id"}),
                        }
                    ],
                }
            ]
        },
    }
    stream[3]["summary"] = '{"wrong":"shape"}'
    stop = {
        "type": "result",
        "subtype": "error_max_budget_usd",
        "session_id": "s",
        "is_error": True,
    }
    return stream[:5] + [correction, resumed, started, completed, stop]


def test_budget_stop_with_one_native_ack_repair():
    proof, _ = verify(repaired_events(), allow_budget_stop=True)
    assert proof["agent_id"] == "native-id"
    assert proof["parent_budget_stopped"] is True
    assert proof["acknowledgement_repaired"] is True


def test_budget_stop_without_repair():
    stream = events()
    stream[-1].update(subtype="error_max_budget_usd", is_error=True)
    proof, _ = verify(stream, allow_budget_stop=True)
    assert proof["parent_budget_stopped"] is True
    assert proof["acknowledgement_repaired"] is False


def test_budget_stop_default_remains_strict():
    with pytest.raises(ValueError):
        verify(repaired_events())


@pytest.mark.parametrize("index", [2, 3, 4, 6, 7, 8, 9])
def test_budget_repair_requires_complete_native_chain(index):
    stream = repaired_events()
    del stream[index]
    with pytest.raises(ValueError):
        verify(stream, allow_budget_stop=True)


@pytest.mark.parametrize(
    "index,key,value",
    [
        (7, "task_id", "wrong"),
        (8, "task_id", "wrong"),
        (9, "subtype", "error_during_execution"),
        (8, "summary", '{"wrong":"still wrong"}'),
    ],
)
def test_budget_repair_rejects_mismatch(index, key, value):
    stream = repaired_events()
    stream[index][key] = value
    with pytest.raises(ValueError):
        verify(stream, allow_budget_stop=True)


def test_budget_repair_rejects_wrong_recipient():
    stream = repaired_events()
    stream[5]["message"]["content"][0]["input"]["to"] = "wrong"
    with pytest.raises(ValueError):
        verify(stream, allow_budget_stop=True)


def test_budget_repair_rejects_two_corrections():
    stream = repaired_events()
    stream.insert(9, stream[5])
    with pytest.raises(ValueError):
        verify(stream, allow_budget_stop=True)


def test_budget_stop_allows_trailing_identical_reinitialization():
    stream = repaired_events()
    stream.extend([stream[0].copy(), stream[-1].copy()])
    proof, _ = verify(stream, allow_budget_stop=True)
    assert proof["parent_budget_stopped"] is True


@pytest.mark.parametrize(
    "key,value",
    [
        ("model", "wrong"),
        ("cwd", "D:/wrong"),
        ("session_id", "wrong"),
        ("claude_code_version", "wrong"),
    ],
)
def test_budget_trailing_init_identity_required(key, value):
    stream = repaired_events()
    trailing = stream[0].copy()
    trailing[key] = value
    stream.extend([trailing, stream[-1].copy()])
    with pytest.raises(ValueError):
        verify(stream, allow_budget_stop=True)


def test_budget_trailing_init_cannot_reopen_activity():
    stream = repaired_events()
    stream.extend(
        [
            stream[0].copy(),
            {
                "type": "assistant",
                "session_id": "s",
                "message": {"content": [{"type": "text", "text": "more"}]},
            },
            stream[-1].copy(),
        ]
    )
    with pytest.raises(ValueError):
        verify(stream, allow_budget_stop=True)


@pytest.mark.parametrize("index", [7, 8])
def test_budget_correction_cannot_change_native_type(index):
    stream = repaired_events()
    stream[index]["subagent_type"] = "wrong"
    with pytest.raises(ValueError):
        verify(stream, allow_budget_stop=True)
