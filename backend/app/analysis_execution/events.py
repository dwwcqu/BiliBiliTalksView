"""Parse untrusted CLI JSONL without promoting transport evidence to acceptance."""

from decimal import Decimal

from app.analysis_packets.codec import json_bytes, loads


def _outcome(error: str | None, result: dict | None = None, cost: str | None = None) -> dict:
    return {
        "status": "failed" if error else "transport_succeeded",
        "error_code": error,
        "result": result if error is None else None,
        "estimated_cost_usd": cost if error is None else None,
        "cost_status": "estimated" if error is None and cost is not None else "unknown",
    }


def parse_events(raw: bytes, *, session_id: str, returncode: int) -> dict:
    """Validate the complete stream; costs are estimates, never billed amounts."""
    try:
        events = [loads(line) for line in raw.decode("utf-8").split("\n") if line.strip()]
    except (UnicodeError, ValueError, RecursionError):
        return _outcome("invalid_event_json")
    if any(not isinstance(event, dict) for event in events):
        return _outcome("invalid_event_object")

    initialized = False
    terminal = False
    results = []
    for event in events:
        parent = event.get("parent_tool_use_id")
        if parent is not None:
            if not isinstance(parent, str) or not parent:
                return _outcome("invalid_parent_identity")
            # Explicit child envelopes cannot provide the main session's result.
            continue
        if "session_id" in event and event["session_id"] != session_id:
            return _outcome("session_mismatch")
        kind = event.get("type")
        if not isinstance(kind, str) or not kind:
            return _outcome("invalid_event_type")
        is_init = kind == "system" and event.get("subtype") == "init"
        if (is_init or kind == "result") and event.get("session_id") != session_id:
            return _outcome("session_mismatch")
        if is_init:
            initialized = True
            terminal = False
        elif kind == "result":
            if not initialized:
                return _outcome("missing_initialization")
            if event.get("is_error") is not False or event.get("subtype") != "success":
                return _outcome("main_result_failed")
            results.append(event)
            terminal = True
        elif kind in ("assistant", "user", "stream_event"):
            terminal = False
    if returncode != 0:
        return _outcome("process_exit_failed")
    if not results or not terminal:
        return _outcome("missing_terminal_result")

    cost = None
    if len(results) == 1:
        value = results[0].get("total_cost_usd")
        if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
            amount = Decimal(value)
            if amount.is_finite() and amount >= 0:
                cost = str(amount)
    return _outcome(None, results[-1], cost)


def equivalent_message(observed: object, expected: str) -> bool:
    """Ignore JSON object formatting only; retain exact strings and array order."""
    if not isinstance(observed, str):
        return False
    if observed == expected:
        return True
    try:
        left, right = loads(observed), loads(expected)
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and json_bytes(left) == json_bytes(right)
        )
    except (ValueError, TypeError, RecursionError):
        return False
