"""Deployment singleton and short, independently heartbeated task leases."""

import hashlib
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import SQLAlchemyError

from app.comment_export.source import CollectionStopped

from .errors import JobError
from .schema import collection_jobs, resolution_requests, source_runtime

LOCK_KEY = int.from_bytes(hashlib.sha256(b"bilibili-worker:v1").digest()[:8], "big", signed=True)
LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 10


def table_for(kind):
    return collection_jobs if kind == "job" else resolution_requests


def id_column(kind):
    return table_for(kind).c.job_id if kind == "job" else table_for(kind).c.request_id


def assert_owner(conn, token, pid, *, lock=False):
    query = select(source_runtime).where(source_runtime.c.id == 1)
    if lock:
        query = query.with_for_update()
    runtime = conn.execute(query).mappings().one()
    key = LOCK_KEY & ((1 << 64) - 1)
    alive = conn.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
            "AND pid=:pid AND granted AND classid::bigint=:hi AND objid::bigint=:lo AND objsubid=1)"
        ),
        {"pid": pid, "hi": key >> 32, "lo": key & 0xFFFFFFFF},
    )
    if runtime["worker_token"] != token or runtime["owner_backend_pid"] != pid or not alive:
        raise JobError("lease_lost")
    return runtime


def guard_task(conn, claim, token, pid, *, lock=False, check_cancel=True):
    assert_owner(conn, token, pid, lock=lock)
    table = table_for(claim["kind"])
    query = select(table).where(
        id_column(claim["kind"]) == claim["id"],
        table.c.owner_token == claim["owner_token"],
        table.c.status == ("running" if claim["kind"] == "job" else "resolving"),
        table.c.lease_until > func.clock_timestamp(),
    )
    if lock:
        query = query.with_for_update()
    row = conn.execute(query).mappings().one_or_none()
    if row is None:
        raise JobError("lease_lost")
    if check_cancel and claim["kind"] == "job" and row["cancel_requested"]:
        raise JobError("job_cancelled")
    return dict(row)


class WorkerOwner:
    def __init__(self, engine):
        self.engine = engine
        self.token = str(uuid4())
        self.connection = None
        self.pid = None

    def __enter__(self):
        conn = self.engine.connect()
        self.connection = conn
        try:
            if not conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}):
                raise JobError("worker_busy")
            self.pid = conn.scalar(text("SELECT pg_backend_pid()"))
            conn.execute(
                update(source_runtime)
                .where(source_runtime.c.id == 1)
                .values(worker_token=self.token, owner_backend_pid=self.pid, updated_at=func.now())
            )
            conn.commit()
            return self
        except BaseException:
            conn.invalidate()
            conn.close()
            raise

    def __exit__(self, *_):
        conn = self.connection
        try:
            if conn is not None and not conn.closed and not conn.invalidated:
                if conn.in_transaction():
                    conn.rollback()
                conn.execute(
                    update(source_runtime)
                    .where(source_runtime.c.id == 1, source_runtime.c.worker_token == self.token)
                    .values(worker_token=None, owner_backend_pid=None, updated_at=func.now())
                )
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
                conn.commit()
        finally:
            if conn is not None:
                conn.invalidate()
                conn.close()


class TaskLease:
    def __init__(self, owner, claim):
        self.owner, self.claim = owner, claim
        self.stop = threading.Event()
        self.failure = None
        self.thread = None
        self.work_dir = None

    def check(self):
        if self.failure:
            raise CollectionStopped(self.failure)
        try:
            with self.owner.engine.begin() as conn:
                guard_task(conn, self.claim, self.owner.token, self.owner.pid)
        except (JobError, SQLAlchemyError) as exc:
            raise CollectionStopped(getattr(exc, "code", "lease_lost")) from None

    def before_request(self, request):
        if request.url.scheme != "https" or request.url.host != "api.bilibili.com":
            raise CollectionStopped("unsafe_request")
        try:
            with self.owner.engine.begin() as conn:
                row = guard_task(conn, self.claim, self.owner.token, self.owner.pid, lock=True)
                if row["requests"] >= row["max_requests"]:
                    raise JobError("budget_exhausted")
                table = table_for(self.claim["kind"])
                conn.execute(
                    update(table)
                    .where(id_column(self.claim["kind"]) == self.claim["id"])
                    .values(requests=table.c.requests + 1)
                )
        except (JobError, SQLAlchemyError) as exc:
            raise CollectionStopped(getattr(exc, "code", "lease_lost")) from None

    def renew(self):
        with self.owner.engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '5s'"))
            guard_task(conn, self.claim, self.owner.token, self.owner.pid)
            table = table_for(self.claim["kind"])
            values = {
                "heartbeat_at": datetime.now(UTC),
                "lease_until": datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS),
            }
            if self.claim["kind"] == "job" and self.work_dir is not None:
                from app.comment_export.checkpoint import read_control_snapshot

                snapshot = read_control_snapshot(self.work_dir)
                progress, metadata = snapshot["progress"], snapshot["metadata"]
                threads = metadata.get("_threads", {})
                phase = (
                    "import"
                    if progress.get("finished")
                    else "replies"
                    if progress.get("main_done")
                    else "main"
                )
                values.update(
                    phase=phase,
                    progress={
                        "phase": phase,
                        "requests": progress.get("requests", 0),
                        "comments": snapshot["comment_count"],
                        "threads": len(threads),
                        "verified_threads": sum(
                            s.get("reply_check_state") == "complete" for s in threads.values()
                        ),
                        "unavailable_threads": sum(
                            bool(s.get("unavailable")) for s in threads.values()
                        ),
                        "updated_at": datetime.now(UTC).isoformat(),
                    },
                )
            conn.execute(
                update(table)
                .where(id_column(self.claim["kind"]) == self.claim["id"])
                .values(**values)
            )

    def _heartbeat(self):
        while not self.stop.wait(HEARTBEAT_SECONDS):
            try:
                self.renew()
            except (JobError, SQLAlchemyError, OSError, ValueError) as exc:
                self.failure = getattr(exc, "code", "lease_lost")
                return

    def __enter__(self):
        self.check()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=6)
