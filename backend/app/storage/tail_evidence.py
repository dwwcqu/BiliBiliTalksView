"""Optional pagination evidence is usable only when it matches the saved state."""
from datetime import datetime


def tail_for_state(state: dict, root: dict | None, rows: dict, snapshot_at: str) -> dict | None:
    value = state.get('tail_evidence')
    if value is None or root is None:
        return None
    from app.comment_export.tail import validate_tail_evidence

    result = validate_tail_evidence(value, root, rows, snapshot_at=snapshot_at)
    if result is None:
        return None
    try:
        if (
            state.get('reply_check_state') != 'complete'
            or state.get('unavailable')
            or result['source_count'] != state.get('count')
            or type(state.get('checked_count')) is not int
            or not 0 <= state['checked_count'] <= result['source_count']
            or result['root_signature'] != state.get('checked_root')
            or datetime.fromisoformat(result['last_full_checked_at'])
            != datetime.fromisoformat(state['last_complete_at'])
        ):
            return None
    except (KeyError, ValueError, TypeError):
        return None
    return result
