import pytest
from sqlalchemy import text

from app.storage.errors import StorageError
from app.storage.locks import lock_key, video_lock


def test_acquire_commit_failure_closes_connection_and_releases_lock(db_engine, monkeypatch):
    first = db_engine.connect()

    def fail_commit():
        raise RuntimeError("injected_commit_failure")

    monkeypatch.setattr(first, "commit", fail_commit)
    with (
        pytest.raises(RuntimeError, match="injected_commit_failure"),
        video_lock(first, "bilibili:video:1"),
    ):
        pytest.fail("ownership must not be yielded")
    assert first.closed
    with db_engine.connect() as second, video_lock(second, "bilibili:video:1"):
        assert not second.closed


def test_false_unlock_closes_connection_and_releases_lock(db_engine, monkeypatch):
    first = db_engine.connect()
    original = first.scalar

    def false_unlock(statement, *args, **kwargs):
        if "pg_advisory_unlock" in str(statement):
            return False
        return original(statement, *args, **kwargs)

    monkeypatch.setattr(first, "scalar", false_unlock)
    with video_lock(first, "bilibili:video:1"):
        pass
    assert first.closed
    with db_engine.connect() as second, video_lock(second, "bilibili:video:1"):
        assert not second.closed


def test_session_lock_survives_transactions_and_busy_is_immediate(db_engine):
    with db_engine.connect() as first, db_engine.connect() as second:
        with video_lock(first, "bilibili:video:1"):
            with first.begin():
                first.execute(text("SELECT 1"))
            with (
                pytest.raises(StorageError, match="import_busy"),
                video_lock(second, "bilibili:video:1"),
            ):
                pytest.fail("second connection acquired active ownership")
        with video_lock(second, "bilibili:video:1"):
            pass


def test_body_failure_releases_lock(db_engine):
    with db_engine.connect() as first, db_engine.connect() as second:
        with (
            pytest.raises(ValueError, match="injected_body"),
            video_lock(first, "bilibili:video:1"),
        ):
            first.execute(text("SELECT 1"))
            raise ValueError("injected_body")
        assert second.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key("bilibili:video:1")}
        )
        second.execute(
            text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key("bilibili:video:1")}
        )
        second.commit()


@pytest.mark.parametrize("failure", ["unlock", "release_commit"])
def test_release_failure_closes_connection(db_engine, monkeypatch, failure):
    with db_engine.connect() as first:
        with (
            pytest.raises(RuntimeError, match="injected_release"),
            video_lock(first, "bilibili:video:1"),
        ):

            def fail(*args, **kwargs):
                raise RuntimeError("injected_release")

            monkeypatch.setattr(first, "scalar" if failure == "unlock" else "commit", fail)
        assert first.closed
    with db_engine.connect() as second, video_lock(second, "bilibili:video:1"):
        pass
