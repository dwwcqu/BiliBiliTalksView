"""Persistent API quotas are exact and transactional in PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, insert, select

from app.api.rate_limits import RateLimited, charge
from app.api.schema import api_rate_limits

CLIENT_A = "a" * 64
CLIENT_B = "b" * 64


def _hits(connection, client_key=CLIENT_A):
    return connection.scalar(
        select(func.coalesce(func.sum(api_rate_limits.c.hits), 0)).where(
            api_rate_limits.c.client_key == client_key
        )
    )


def test_concurrent_connections_enforce_the_exact_cap(db_engine):
    now = datetime(2026, 9, 6, 3, 14, 15, tzinfo=UTC)
    barrier = Barrier(16)

    def attempt(_):
        barrier.wait()
        try:
            with db_engine.begin() as connection:
                charge(connection, CLIENT_A, "write", now=now, limit=7)
            return "charged"
        except RateLimited as error:
            assert error.kind == "write"
            assert error.retry_after == 45
            return "limited"

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(attempt, range(16)))

    assert outcomes.count("charged") == 7
    assert outcomes.count("limited") == 9
    with db_engine.connect() as connection:
        assert _hits(connection) == 7


def test_rolled_back_charge_does_not_consume_quota(db_engine):
    now = datetime(2026, 9, 6, 3, 14, tzinfo=UTC)
    with db_engine.connect() as connection:
        transaction = connection.begin()
        charge(connection, CLIENT_A, "write", now=now, limit=1)
        transaction.rollback()

    with db_engine.begin() as connection:
        charge(connection, CLIENT_A, "write", now=now, limit=1)
    with db_engine.connect() as connection:
        assert _hits(connection) == 1


def test_clients_have_independent_quotas(db_engine):
    now = datetime(2026, 9, 6, 3, 14, tzinfo=UTC)
    with db_engine.begin() as connection:
        charge(connection, CLIENT_A, "intent", now=now, limit=1)
        charge(connection, CLIENT_B, "intent", now=now, limit=1)
        with pytest.raises(RateLimited):
            charge(connection, CLIENT_A, "intent", now=now, limit=1)

    with db_engine.connect() as connection:
        assert _hits(connection, CLIENT_A) == 1
        assert _hits(connection, CLIENT_B) == 1


@pytest.mark.parametrize(
    ("kind", "before", "after", "retry_after"),
    [
        (
            "write",
            datetime(2026, 9, 6, 3, 14, 59, 100_000, tzinfo=UTC),
            datetime(2026, 9, 6, 3, 15, tzinfo=UTC),
            1,
        ),
        (
            "intent",
            datetime(2026, 9, 6, 3, 59, 59, 100_000, tzinfo=UTC),
            datetime(2026, 9, 6, 4, 0, tzinfo=UTC),
            1,
        ),
    ],
)
def test_natural_window_boundary_starts_a_new_bucket(db_engine, kind, before, after, retry_after):
    with db_engine.begin() as connection:
        charge(connection, CLIENT_A, kind, now=before, limit=1)
        with pytest.raises(RateLimited) as caught:
            charge(connection, CLIENT_A, kind, now=before, limit=1)
        charge(connection, CLIENT_A, kind, now=after, limit=1)

    assert caught.value.retry_after == retry_after
    with db_engine.connect() as connection:
        assert _hits(connection) == 2


def test_charge_removes_buckets_older_than_48_hours(db_engine):
    now = datetime(2026, 9, 6, 12, tzinfo=UTC)
    with db_engine.begin() as connection:
        connection.execute(
            insert(api_rate_limits),
            [
                {
                    "client_key": CLIENT_A,
                    "kind": "write",
                    "window_start": now - timedelta(hours=49),
                    "hits": 2,
                },
                {
                    "client_key": CLIENT_B,
                    "kind": "intent",
                    "window_start": now - timedelta(hours=47),
                    "hits": 2,
                },
            ],
        )
        charge(connection, "c" * 64, "write", now=now)

    with db_engine.connect() as connection:
        starts = set(connection.scalars(select(api_rate_limits.c.window_start)))
    assert now - timedelta(hours=49) not in starts
    assert now - timedelta(hours=47) in starts


@pytest.mark.parametrize(
    ("client_key", "kind", "now", "limit"),
    [
        ("A" * 64, "write", None, 1),
        ("a" * 63, "write", None, 1),
        (CLIENT_A, "read", None, 1),
        (CLIENT_A, "write", datetime(2026, 9, 6, tzinfo=UTC).replace(tzinfo=None), 1),
        (CLIENT_A, "write", None, 0),
        (CLIENT_A, "write", None, True),
    ],
)
def test_charge_rejects_invalid_parameters(db_engine, client_key, kind, now, limit):
    with db_engine.begin() as connection, pytest.raises(ValueError):
        charge(connection, client_key, kind, now=now, limit=limit)


def test_default_limits_are_write_minute_and_intent_hour(db_engine):
    now = datetime(2026, 9, 6, 3, 0, tzinfo=UTC)
    with db_engine.begin() as connection:
        for _ in range(30):
            charge(connection, CLIENT_A, "write", now=now)
        for _ in range(3):
            charge(connection, CLIENT_A, "intent", now=now)
        with pytest.raises(RateLimited):
            charge(connection, CLIENT_A, "write", now=now)
        with pytest.raises(RateLimited):
            charge(connection, CLIENT_A, "intent", now=now)
