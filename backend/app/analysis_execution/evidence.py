"""Pinned native CLI delivery evidence, independent of generated prose."""

import hashlib
from pathlib import Path

from app.analysis_execution.events import equivalent_message, parse_events
from app.analysis_packets.codec import loads


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def _blocks(event: dict) -> list:
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content", [])
    _require(isinstance(content, list), "invalid_native_content")
    _require(all(isinstance(block, dict) for block in content), "invalid_native_content")
    return content


def _json_object(value: object) -> dict:
    _require(isinstance(value, str), "invalid_native_json")
    try:
        parsed = loads(value)
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid_native_json") from exc
    _require(isinstance(parsed, dict), "invalid_native_json")
    return parsed


def verify_delivery(
    raw: bytes,
    *,
    session_id: str,
    agent_id: str,
    message: str,
    delivery_sha256: str,
    expected_model: str,
    cli_version: str,
    cwd: str,
) -> dict:
    """Return only a matched native notification's strict JSON business result."""
    _require(cli_version == "2.1.261", "unsupported_evidence_adapter")
    transport = parse_events(raw, session_id=session_id, returncode=0)
    _require(
        transport["status"] == "transport_succeeded",
        transport.get("error_code") or "invalid_transport",
    )
    events = [loads(line) for line in raw.decode("utf-8").split("\n") if line.strip()]
    initialized = False
    terminal = False
    tool_id = None
    resumed = False
    started = False
    completed = None
    for event in events:
        kind = event.get("type")
        blocks = _blocks(event) if kind in ("assistant", "user") else []
        child = event.get("parent_tool_use_id") is not None
        if child:
            _require(
                not any(
                    block.get("type") == "tool_use"
                    and block.get("name") in ("Agent", "Task", "SendMessage")
                    for block in blocks
                ),
                "nested_dispatch",
            )
            continue
        subtype = event.get("subtype")
        if kind == "system" and subtype == "init":
            _require(not initialized and tool_id is None, "duplicate_initialization")
            _require(event.get("model") == expected_model, "model_mismatch")
            _require(event.get("claude_code_version") == cli_version, "cli_version_mismatch")
            observed_cwd = event.get("cwd")
            _require(isinstance(observed_cwd, str) and bool(observed_cwd), "cwd_mismatch")
            _require(Path(observed_cwd).resolve() == Path(cwd).resolve(), "cwd_mismatch")
            initialized = True
        elif kind == "result":
            # Background runs can emit successful intermediate and repeated results.
            # Only a result after the matched notification closes delivery evidence.
            terminal = completed is not None
        for block in blocks:
            if block.get("type") == "tool_use":
                _require(kind == "assistant", "invalid_tool_role")
                name = block.get("name")
                _require(name not in ("Agent", "Task"), "unexpected_agent_creation")
                if name != "SendMessage":
                    continue
                _require(initialized and tool_id is None and not terminal, "extra_dispatch")
                _require(event.get("session_id") == session_id, "session_mismatch")
                data = block.get("input")
                _require(isinstance(data, dict), "invalid_dispatch_input")
                recipients = [data[key] for key in ("to", "recipient") if key in data]
                messages = [data[key] for key in ("message", "content") if key in data]
                _require(
                    bool(recipients) and all(v == agent_id for v in recipients),
                    "recipient_mismatch",
                )
                _require(
                    bool(messages) and all(equivalent_message(v, message) for v in messages),
                    "delivery_message_mismatch",
                )
                _require("type" not in data or data["type"] == "message", "invalid_dispatch_type")
                tool_id = block.get("id")
                _require(isinstance(tool_id, str) and bool(tool_id), "invalid_tool_identity")
            elif block.get("type") == "tool_result" and block.get("tool_use_id") == tool_id:
                _require(kind == "user", "invalid_tool_result_role")
                _require(
                    tool_id is not None and not resumed and completed is None,
                    "invalid_resume_order",
                )
                _require(event.get("session_id") == session_id, "session_mismatch")
                _require(block.get("is_error", False) is False, "resume_failed")
                content = block.get("content")
                _require(
                    isinstance(content, list)
                    and len(content) == 1
                    and isinstance(content[0], dict)
                    and content[0].get("type") == "text",
                    "invalid_resume_result",
                )
                result = _json_object(content[0].get("text"))
                _require(
                    result.get("success") is True and result.get("resumedAgentId") == agent_id,
                    "resume_identity_mismatch",
                )
                resumed = True
        if kind == "system" and subtype in ("task_started", "task_notification"):
            _require(initialized and tool_id is not None and not terminal, "invalid_task_order")
            _require(
                event.get("session_id") == session_id
                and event.get("task_id") == agent_id
                and event.get("tool_use_id") == tool_id,
                "task_identity_mismatch",
            )
            if subtype == "task_started":
                _require(not started and completed is None, "duplicate_task_started")
                _require(
                    type(event.get("spawn_depth")) is int
                    and event["spawn_depth"] == 1
                    and event.get("task_type") == "local_agent",
                    "invalid_task_kind",
                )
                started = True
            else:
                _require(started and resumed and completed is None, "invalid_completion_order")
                _require(event.get("status") == "completed", "native_task_failed")
                result = _json_object(event.get("summary"))
                _require(
                    result.get("delivery_sha256") == delivery_sha256, "delivery_digest_mismatch"
                )
                completed = result.get("task_result")
                _require(isinstance(completed, dict), "invalid_task_result")
    _require(terminal and completed is not None, "missing_delivery_evidence")
    return {
        "task_result": completed,
        "tool_use_id": tool_id,
        "agent_id": agent_id,
        "stdout_sha256": hashlib.sha256(raw).hexdigest(),
    }
