"""Fresh synthetic native delivery evidence; never contact a model."""

import hashlib
import json

import pytest

from app.analysis_execution.evidence import verify_delivery


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
                        "name": "SendMessage",
                        "input": {"to": "a", "message": "exact\u2028message"},
                    }
                ]
            },
        },
        {
            "type": "system",
            "subtype": "task_started",
            "session_id": "s",
            "task_id": "a",
            "tool_use_id": "call",
            "spawn_depth": 1,
            "task_type": "local_agent",
        },
        {
            "type": "user",
            "session_id": "s",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call",
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps({"success": True, "resumedAgentId": "a"}),
                            }
                        ],
                    }
                ]
            },
        },
        {
            "type": "system",
            "subtype": "task_notification",
            "session_id": "s",
            "task_id": "a",
            "tool_use_id": "call",
            "status": "completed",
            "summary": json.dumps({"delivery_sha256": "digest", "task_result": {"accepted": True}}),
        },
        {"type": "result", "subtype": "success", "session_id": "s", "is_error": False},
    ]


def verify(stream, message="exact\u2028message"):
    raw = b"\n".join(json.dumps(e, ensure_ascii=False).encode() for e in stream)
    return verify_delivery(
        raw,
        session_id="s",
        agent_id="a",
        message=message,
        delivery_sha256="digest",
        expected_model="m",
        cli_version="2.1.261",
        cwd="D:/work",
    ), raw


def test_native_delivery_and_original_child_parent():
    stream = events()
    stream.insert(
        4,
        {
            "type": "assistant",
            "session_id": "child",
            "parent_tool_use_id": "original-creation",
            "message": {"content": [{"type": "text", "text": "untrusted child"}]},
        },
    )
    proof, raw = verify(stream)
    assert proof == {
        "task_result": {"accepted": True},
        "agent_id": "a",
        "tool_use_id": "call",
        "stdout_sha256": hashlib.sha256(raw).hexdigest(),
    }


@pytest.mark.parametrize(
    "index,key,value",
    [
        (0, "model", "wrong"),
        (0, "claude_code_version", "2.1.262"),
        (0, "cwd", "D:/elsewhere"),
        (0, "session_id", "wrong"),
        (2, "task_id", "wrong"),
        (2, "tool_use_id", "wrong"),
        (2, "spawn_depth", 2),
        (2, "task_type", "other"),
        (4, "task_id", "wrong"),
        (4, "tool_use_id", "wrong"),
        (4, "status", "failed"),
        (4, "session_id", "wrong"),
        (4, "summary", "```json\n{}\n```"),
        (4, "summary", "{}"),
        (4, "summary", '{"delivery_sha256":"old","task_result":{}}'),
        (4, "summary", '{"delivery_sha256":"digest","task_result":[]}'),
    ],
)
def test_mismatched_native_evidence(index, key, value):
    stream = events()
    stream[index][key] = value
    with pytest.raises(ValueError):
        verify(stream)


@pytest.mark.parametrize("missing", range(6))
def test_each_proof_step_required(missing):
    stream = events()
    del stream[missing]
    with pytest.raises(ValueError):
        verify(stream)


@pytest.mark.parametrize(
    "mode",
    [
        "extra",
        "agent",
        "recipient",
        "message",
        "content",
        "error",
        "resume",
        "order",
        "spoof",
        "nested",
        "duplicate",
    ],
)
def test_dispatch_and_order_rejections(mode):
    stream = events()
    call = stream[1]["message"]["content"][0]
    result = stream[3]["message"]["content"][0]
    if mode == "extra":
        stream.insert(2, stream[1])
    elif mode == "agent":
        call["name"] = "Agent"
    elif mode in ("recipient", "content"):
        call["input"][mode] = "wrong"
    elif mode == "message":
        call["input"]["message"] += "changed"
    elif mode == "error":
        result["is_error"] = True
    elif mode == "resume":
        result["content"][0]["text"] = '{"success":true,"resumedAgentId":"other"}'
    elif mode == "order":
        stream[3], stream[4] = stream[4], stream[3]
    elif mode == "spoof":
        stream[4]["parent_tool_use_id"] = "child"
    elif mode == "nested":
        stream.insert(
            4,
            {
                "type": "assistant",
                "parent_tool_use_id": "child",
                "message": {
                    "content": [{"type": "tool_use", "name": "Agent", "id": "nested", "input": {}}]
                },
            },
        )
    else:
        stream.insert(4, stream[2])
    with pytest.raises(ValueError):
        verify(stream)


def test_legacy_aliases_and_resume_before_started():
    stream = events()
    data = stream[1]["message"]["content"][0]["input"]
    data["recipient"] = data.pop("to")
    data["content"] = data.pop("message")
    data["type"] = "message"
    stream[2], stream[3] = stream[3], stream[2]
    assert verify(stream)[0]["task_result"] == {"accepted": True}


@pytest.mark.parametrize(
    "tail", [b"\n{", b"\n[]", b"\nnull", b"\n\xff", b'\n{"type":"x","type":"y"}']
)
def test_entire_stream_must_parse(tail):
    _, raw = verify(events())
    with pytest.raises(ValueError):
        verify_delivery(
            raw + tail,
            session_id="s",
            agent_id="a",
            message="exact\u2028message",
            delivery_sha256="digest",
            expected_model="m",
            cli_version="2.1.261",
            cwd="D:/work",
        )


@pytest.mark.parametrize("role_index,role", [(1, "user"), (3, "assistant")])
def test_tool_evidence_requires_native_roles(role_index, role):
    stream = events()
    stream[role_index]["type"] = role
    with pytest.raises(ValueError):
        verify(stream)


def test_pinned_adapter_required_even_if_init_version_matches():
    stream = events()
    stream[0]["claude_code_version"] = "2.1.262"
    raw = b"\n".join(json.dumps(event).encode() for event in stream)
    with pytest.raises(ValueError, match="unsupported_evidence_adapter"):
        verify_delivery(
            raw,
            session_id="s",
            agent_id="a",
            message="exact\u2028message",
            delivery_sha256="digest",
            expected_model="m",
            cli_version="2.1.262",
            cwd="D:/work",
        )


def test_multiple_main_success_results_after_native_completion():
    stream = events()
    stream.extend([dict(stream[-1]), dict(stream[-1])])
    assert verify(stream)[0]["task_result"] == {"accepted": True}


def test_intermediate_main_success_does_not_replace_final_completion_result():
    stream = events()
    stream.insert(2, dict(stream[-1]))
    assert verify(stream)[0]["task_result"] == {"accepted": True}
    stream.pop()
    with pytest.raises(ValueError):
        verify(stream)


def test_intermediate_failed_main_result_cannot_be_hidden():
    stream = events()
    stream.insert(2, {**stream[-1], "is_error": True})
    with pytest.raises(ValueError):
        verify(stream)


JSON_MESSAGE = '{"comments":["exact  comment\\ntext","second"],"instructions":"read"}\n'


def test_json_message_formatting_preserves_all_fields():
    stream = events()
    stream[1]["message"]["content"][0]["input"]["message"] = json.dumps(
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
    stream[1]["message"]["content"][0]["input"]["message"] = observed
    with pytest.raises(ValueError):
        verify(stream, message=JSON_MESSAGE)
