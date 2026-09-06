"""Transaction-scoped persistent API rate limits."""

import math
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.schema import api_rate_limits

_CLIENT_KEY = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS = {"write": timedelta(minutes=1), "intent": timedelta(hours=1)}
_DEFAULT_LIMITS = {"write": 30, "intent": 3}


class RateLimited(Exception):
    """Raised when a client has exhausted one quota window."""

    def __init__(self, kind: str, retry_after: int):
        self.kind = kind
        self.retry_after = retry_after
        super().__init__(f"{kind}_rate_limited")


def _utc_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware_datetime_required")
    return value.astimezone(UTC)


def _window_start(stamp: datetime, kind: str) -> datetime:
    if kind == "write":
        return stamp.replace(second=0, microsecond=0)
    return stamp.replace(minute=0, second=0, microsecond=0)


def charge(conn, client_key, kind, *, now=None, limit=None) -> None:
    """Charge one request in the caller's current transaction."""
    if not isinstance(client_key, str) or _CLIENT_KEY.fullmatch(client_key) is None:
        raise ValueError("invalid_client_key")
    if kind not in _WINDOWS:
        raise ValueError("invalid_rate_limit_kind")
    if limit is None:
        limit = _DEFAULT_LIMITS[kind]
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("invalid_rate_limit")

    stamp = _utc_now(now)
    window_start = _window_start(stamp, kind)
    conn.execute(
        delete(api_rate_limits).where(api_rate_limits.c.window_start < stamp - timedelta(hours=48))
    )
    statement = (
        pg_insert(api_rate_limits)
        .values(client_key=client_key, kind=kind, window_start=window_start, hits=1)
        .on_conflict_do_update(
            index_elements=[
                api_rate_limits.c.client_key,
                api_rate_limits.c.kind,
                api_rate_limits.c.window_start,
            ],
            set_={"hits": api_rate_limits.c.hits + 1},
            where=api_rate_limits.c.hits < limit,
        )
        .returning(api_rate_limits.c.hits)
    )
    if conn.execute(statement).scalar_one_or_none() is None:
        end = window_start + _WINDOWS[kind]
        raise RateLimited(kind, max(1, math.ceil((end - stamp).total_seconds())))
