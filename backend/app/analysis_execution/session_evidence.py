"""Strict evidence for pinned ordinary CLI sessions, separate from native agents."""

from pathlib import Path
from uuid import UUID

from app.analysis_execution.events import parse_events
from app.analysis_packets.codec import loads


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def _inspect(value: object) -> None:
    """Reject nested execution and tool errors, including streamed content blocks."""
    if isinstance(value, list):
        for item in value:
            _inspect(item)
    elif isinstance(value, dict):
        _require(value.get("parent_tool_use_id") is None, "nested_execution")
        _require(
            not any(
                value.get(key) is not None
                for key in ("task_id", "agent_id", "parent_agent_id", "spawn_depth")
            ),
            "nested_execution",
        )
        _require(
            not (
                value.get("type") == "system" and str(value.get("subtype", "")).startswith("task_")
            ),
            "nested_execution",
        )
        _require(not value.get("error") and not value.get("errors"), "session_event_error")
        _require(value.get("is_error", False) is False, "session_event_error")
        _require(value.get("type") != "error", "session_event_error")
        _require(
            value.get("subtype") not in ("task_started", "task_notification"), "nested_execution"
        )
        _require(not str(value.get("subtype", "")).startswith("error"), "session_event_error")
        if value.get("type") == "tool_use":
            _require(value.get("name") == "Read", "unexpected_session_tool")
        for key, item in value.items():
            # Tool input and tool-return payload are data rather than event envelopes.
            if key != "input" and not (value.get("type") == "tool_result" and key == "content"):
                _inspect(item)


def verify_session_output(
    raw: bytes,
    *,
    session_id: str,
    expected_model: str,
    cli_version: str,
    cwd: str,
) -> dict:
    """Return only the unique final main result's strict JSON object."""
    _require(cli_version == "2.1.261", "unsupported_evidence_adapter")
    try:
        _require(str(UUID(session_id)) == session_id, "invalid_session_uuid")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("invalid_session_uuid") from exc
    transport = parse_events(raw, session_id=session_id, returncode=0)
    _require(
        transport["status"] == "transport_succeeded",
        transport.get("error_code") or "invalid_transport",
    )
    events = [loads(line) for line in raw.decode("utf-8").split("\n") if line.strip()]
    initialized = False
    result = None
    for event in events:
        _require(result is None, "event_after_terminal_result")
        _inspect(event)
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            _require(not initialized, "duplicate_initialization")
            _require(event.get("model") == expected_model, "model_mismatch")
            _require(event.get("claude_code_version") == cli_version, "cli_version_mismatch")
            observed_cwd = event.get("cwd")
            _require(isinstance(observed_cwd, str) and bool(observed_cwd), "cwd_mismatch")
            _require(Path(observed_cwd).resolve() == Path(cwd).resolve(), "cwd_mismatch")
            initialized = True
        else:
            _require(initialized, "event_before_initialization")
        if kind == "result":
            value = event.get("result")
            _require(isinstance(value, str), "invalid_session_json")
            try:
                result = loads(value)
            except (ValueError, RecursionError) as exc:
                raise ValueError("invalid_session_json") from exc
            _require(isinstance(result, dict), "invalid_session_json")
    _require(result is not None, "missing_terminal_result")
    return result
