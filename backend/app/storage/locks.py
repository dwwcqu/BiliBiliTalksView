"""Session ownership stays on the same dedicated connection across transactions."""

import hashlib
from contextlib import contextmanager

from sqlalchemy import text

from .errors import StorageError


def lock_key(video_id: str) -> int:
    return int.from_bytes(
        hashlib.sha256(("bilibili-import:" + video_id).encode()).digest()[:8], "big", signed=True
    )


def _close_failed_connection(conn) -> None:
    # Never leave an uncertain session alive or return its connection to a pool.
    try:
        conn.invalidate()
    finally:
        conn.close()


@contextmanager
def video_lock(conn, video_id: str):
    if conn.in_transaction():
        raise StorageError("transaction_already_active")
    key = lock_key(video_id)
    try:
        acquired = conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        conn.commit()
    except BaseException:
        # Acquisition can succeed at the server even if the response/commit fails locally.
        _close_failed_connection(conn)
        raise
    if not acquired:
        raise StorageError("import_busy")
    try:
        yield
    finally:
        try:
            if conn.closed or conn.invalidated:
                _close_failed_connection(conn)
            else:
                if conn.in_transaction():
                    conn.rollback()
                released = conn.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                if released is not True:
                    _close_failed_connection(conn)
                else:
                    conn.commit()
        except BaseException:
            _close_failed_connection(conn)
            raise
