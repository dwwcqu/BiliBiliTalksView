"""Pinned native member-creation evidence; generated IDs are never trusted."""

import hashlib
import re
from pathlib import Path

from app.analysis_execution.events import equivalent_message, parse_events
from app.analysis_execution.evidence import _blocks, _json_object, _require
from app.analysis_packets.codec import json_bytes, loads


def _safe_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None


def verify_creation(
    raw: bytes,
    *,
    session_id: str,
    member_id: str,
    role: str,
    native_type: str,
    message: str,
    initialization_sha256: str,
    expected_model: str,
    cli_version: str,
    cwd: str,
    allow_budget_stop: bool = False,
) -> dict:
    """Validate one exact Agent creation, native identity, and strict handshake."""
    _require(cli_version == "2.1.261", "unsupported_evidence_adapter")
    checked_raw = raw
    budget_stopped = False
    if allow_budget_stop:
        try:
            checked_events = [
                loads(line) for line in raw.decode("utf-8").split("\n") if line.strip()
            ]
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ValueError("invalid_event_json") from exc
        for event in checked_events:
            _require(isinstance(event, dict), "invalid_event_object")
            if (
                event.get("parent_tool_use_id") is None
                and event.get("type") == "result"
                and event.get("subtype") == "error_max_budget_usd"
                and event.get("is_error") is True
            ):
                budget_stopped = True
                event.update(subtype="success", is_error=False)
        # Only transport shape is checked through this copy. Evidence and digest use raw.
        checked_raw = b"".join(json_bytes(event) for event in checked_events)
    transport = parse_events(checked_raw, session_id=session_id, returncode=0)
    _require(
        transport["status"] == "transport_succeeded",
        transport.get("error_code") or "invalid_transport",
    )
    events = [loads(line) for line in raw.decode("utf-8").split("\n") if line.strip()]
    initialized = completed = tool_succeeded = terminal = False
    tool_id = agent_id = correction_id = None
    correction_started = correction_succeeded = correction_completed = False
    acknowledgement = None
    saw_budget_stop = trailing_init = False
    for event in events:
        kind, subtype = event.get("type"), event.get("subtype")
        if trailing_init:
            _require(
                kind == "result" and subtype == "error_max_budget_usd",
                "activity_after_budget_reinitialization",
            )
        blocks = _blocks(event) if kind in ("assistant", "user") else []
        if event.get("parent_tool_use_id") is not None:
            _require(
                not any(
                    block.get("type") == "tool_use"
                    and block.get("name") in ("Agent", "Task", "SendMessage")
                    for block in blocks
                ),
                "nested_dispatch",
            )
            continue
        if kind == "system" and subtype == "init":
            repeated = initialized
            _require(
                not repeated
                or (allow_budget_stop and saw_budget_stop and terminal and not trailing_init),
                "duplicate_initialization",
            )
            if repeated:
                trailing_init = True
            _require(event.get("model") == expected_model, "model_mismatch")
            _require(event.get("claude_code_version") == cli_version, "cli_version_mismatch")
            observed = event.get("cwd")
            _require(isinstance(observed, str) and bool(observed), "cwd_mismatch")
            _require(Path(observed).resolve() == Path(cwd).resolve(), "cwd_mismatch")
            initialized = True
        elif kind == "result":
            if subtype == "error_max_budget_usd":
                saw_budget_stop = True
            terminal = (
                completed
                and tool_succeeded
                and (correction_id is None or (correction_completed and correction_succeeded))
            )
        for block in blocks:
            if block.get("type") == "tool_use":
                _require(kind == "assistant", "invalid_tool_role")
                name = block.get("name")
                _require(name != "Task", "extra_dispatch")
                if name == "SendMessage":
                    _require(
                        allow_budget_stop
                        and correction_id is None
                        and agent_id is not None
                        and completed
                        and tool_succeeded,
                        "extra_dispatch",
                    )
                    _require(event.get("session_id") == session_id, "session_mismatch")
                    data = block.get("input")
                    _require(isinstance(data, dict), "invalid_correction_input")
                    recipients = [data[key] for key in ("to", "recipient") if key in data]
                    messages = [data[key] for key in ("message", "content") if key in data]
                    _require(
                        bool(recipients) and all(v == agent_id for v in recipients),
                        "correction_recipient_mismatch",
                    )
                    _require(
                        bool(messages) and all(isinstance(v, str) and v for v in messages),
                        "invalid_correction_message",
                    )
                    _require(
                        "type" not in data or data["type"] == "message", "invalid_correction_type"
                    )
                    correction_id = block.get("id")
                    _require(
                        _safe_id(correction_id) and correction_id != tool_id,
                        "invalid_correction_identity",
                    )
                    terminal = False
                    continue
                if name != "Agent":
                    continue
                _require(initialized and tool_id is None and not terminal, "extra_dispatch")
                _require(event.get("session_id") == session_id, "session_mismatch")
                data = block.get("input")
                _require(isinstance(data, dict), "invalid_creation_input")
                _require(
                    equivalent_message(data.get("prompt"), message),
                    "initialization_message_mismatch",
                )
                _require(data.get("subagent_type") == native_type, "native_type_mismatch")
                # Inheritance is fixed in the definition; native calls cannot override it.
                # This checks the request, not the child provider's internal model identity.
                _require("model" not in data, "creation_model_override")
                _require(
                    data.get("run_in_background") is False and "resume" not in data,
                    "invalid_creation_mode",
                )
                tool_id = block.get("id")
                _require(_safe_id(tool_id), "invalid_tool_identity")
            elif block.get("type") == "tool_result":
                _require(kind == "user", "invalid_tool_result_role")
                if correction_id is not None and block.get("tool_use_id") == correction_id:
                    _require(
                        not correction_succeeded and not terminal, "duplicate_correction_result"
                    )
                    _require(event.get("session_id") == session_id, "session_mismatch")
                    _require(block.get("is_error", False) is False, "correction_failed")
                    content = block.get("content")
                    _require(
                        isinstance(content, list)
                        and len(content) == 1
                        and isinstance(content[0], dict)
                        and content[0].get("type") == "text",
                        "invalid_correction_result",
                    )
                    resumed = _json_object(content[0].get("text"))
                    _require(
                        resumed.get("success") is True
                        and resumed.get("resumedAgentId") == agent_id,
                        "correction_identity_mismatch",
                    )
                    correction_succeeded = True
                    continue
                _require(
                    tool_id is not None and block.get("tool_use_id") == tool_id,
                    "tool_identity_mismatch",
                )
                _require(not tool_succeeded and not terminal, "duplicate_creation_result")
                _require(event.get("session_id") == session_id, "session_mismatch")
                _require(block.get("is_error", False) is False, "creation_failed")
                content = block.get("content")
                _require(
                    isinstance(content, list)
                    and bool(content)
                    and all(
                        isinstance(item, dict)
                        and item.get("type") == "text"
                        and isinstance(item.get("text"), str)
                        for item in content
                    ),
                    "invalid_creation_result",
                )
                tool_succeeded = True
        if kind == "system" and subtype in ("task_started", "task_notification"):
            _require(initialized and tool_id is not None and not terminal, "invalid_task_order")
            if correction_id is not None and event.get("tool_use_id") == correction_id:
                _require(
                    "subagent_type" not in event or event["subagent_type"] == native_type,
                    "native_type_mismatch",
                )
                _require(
                    event.get("session_id") == session_id and event.get("task_id") == agent_id,
                    "correction_identity_mismatch",
                )
                if subtype == "task_started":
                    _require(
                        not correction_started and not correction_completed,
                        "duplicate_correction_started",
                    )
                    _require(
                        type(event.get("spawn_depth")) is int
                        and event["spawn_depth"] == 1
                        and event.get("task_type") == "local_agent",
                        "invalid_task_kind",
                    )
                    _require(
                        "subagent_type" not in event or event["subagent_type"] == native_type,
                        "native_type_mismatch",
                    )
                    correction_started = True
                else:
                    _require(
                        correction_started and not correction_completed,
                        "invalid_correction_completion_order",
                    )
                    _require(event.get("status") == "completed", "native_task_failed")
                    acknowledgement = _json_object(event.get("summary"))
                    correction_completed = True
                continue
            _require(
                event.get("session_id") == session_id and event.get("tool_use_id") == tool_id,
                "task_identity_mismatch",
            )
            if subtype == "task_started":
                _require(agent_id is None and not completed, "duplicate_task_started")
                agent_id = event.get("task_id")
                _require(_safe_id(agent_id), "invalid_agent_identity")
                _require(event.get("subagent_type") == native_type, "native_type_mismatch")
                _require(
                    type(event.get("spawn_depth")) is int
                    and event["spawn_depth"] == 1
                    and event.get("task_type") == "local_agent",
                    "invalid_task_kind",
                )
            else:
                _require(agent_id is not None and not completed, "invalid_completion_order")
                _require(event.get("task_id") == agent_id, "task_identity_mismatch")
                _require(event.get("status") == "completed", "native_task_failed")
                _require(
                    "subagent_type" not in event or event["subagent_type"] == native_type,
                    "native_type_mismatch",
                )
                if allow_budget_stop:
                    # An initial wrong shape can be repaired once, but cannot establish identity.
                    try:
                        acknowledgement = _json_object(event.get("summary"))
                    except ValueError:
                        acknowledgement = None
                else:
                    acknowledgement = _json_object(event.get("summary"))
                completed = True
    _require(terminal and completed and tool_succeeded, "missing_creation_evidence")
    _require(
        acknowledgement
        == {"initialization_sha256": initialization_sha256, "member_id": member_id, "role": role},
        "handshake_mismatch",
    )
    return {
        "parent_budget_stopped": budget_stopped,
        "acknowledgement_repaired": correction_id is not None,
        "agent_id": agent_id,
        "creation_tool_use_id": tool_id,
        "stdout_sha256": hashlib.sha256(raw).hexdigest(),
    }
