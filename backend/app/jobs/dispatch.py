"""Claim and finish work in short transactions; source calls never hold row locks."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select, update

from app.storage.schema import discussion_states

from .errors import JobError
from .ownership import LEASE_SECONDS, assert_owner, guard_task, id_column, table_for
from .schema import collection_jobs, resolution_requests, source_runtime


def claim_next(owner):
    now = datetime.now(UTC)
    with owner.engine.begin() as conn:
        runtime = assert_owner(conn, owner.token, owner.pid)
        if runtime["action"] == "revalidate":
            return {
                "kind": "revalidate",
                "id": runtime["blocked_id"],
                "target_kind": runtime["blocked_kind"],
            }
        candidates = []
        for kind, table, running in (
            ("job", collection_jobs, "running"),
            ("resolution", resolution_requests, "resolving"),
        ):
            rows = (
                conn.execute(
                    select(table)
                    .where(table.c.status.in_(["queued", "waiting_source", "blocked", running]))
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            for row in rows:
                identity = row["job_id" if kind == "job" else "request_id"]
                if row["status"] == running and row["lease_until"] and row["lease_until"] > now:
                    return None
                recovery_target = (
                    runtime["blocked_kind"] == kind and runtime["blocked_id"] == identity
                )
                if runtime["source_gate"] != "normal" and not recovery_target:
                    continue
                if row["status"] == "blocked" and row["action"] != "recover":
                    continue
                if row["not_before"] is not None and row["not_before"] > now:
                    continue
                stamp = row["requested_at" if kind == "job" else "accepted_at"]
                candidates.append((row["status"] != running, stamp, kind, dict(row)))
        if not candidates:
            return None
        _, _, kind, row = min(candidates, key=lambda value: value[:3])
        table = table_for(kind)
        identity = row["job_id" if kind == "job" else "request_id"]
        token = str(uuid4())
        values = {
            "status": "running" if kind == "job" else "resolving",
            "owner_token": token,
            "lease_until": now + timedelta(seconds=LEASE_SECONDS),
            "heartbeat_at": now,
        }
        if kind == "job":
            values["attempt_count"] = row["attempt_count"] + 1
        conn.execute(update(table).where(id_column(kind) == identity).values(**values))
        if row["action"] == "recover" and runtime["source_gate"] != "normal":
            conn.execute(
                update(source_runtime)
                .where(source_runtime.c.id == 1)
                .values(source_gate="recovering")
            )
        return {**row, **values, "kind": kind, "id": identity}


def complete_job(conn, owner, claim, result):
    row = guard_task(conn, claim, owner.token, owner.pid, lock=True)
    video_id = conn.scalar(
        select(discussion_states.c.video_id).where(
            discussion_states.c.state_id == result["state_id"]
        )
    )
    if video_id != row["video_id"]:
        raise JobError("video_identity_mismatch")
    conn.execute(
        update(collection_jobs)
        .where(collection_jobs.c.job_id == claim["id"])
        .values(
            status="succeeded" if result["published"] else "partial",
            phase="done",
            result_state_id=result["state_id"],
            completed_at=func.now(),
            action=None,
            owner_token=None,
            lease_until=None,
            safe_error=row.get("safe_error"),
        )
    )
    reopen_source(conn, claim)


def reopen_source(conn, claim):
    conn.execute(
        update(source_runtime)
        .where(
            source_runtime.c.id == 1,
            source_runtime.c.blocked_kind == claim["kind"],
            source_runtime.c.blocked_id == claim["id"],
            source_runtime.c.source_gate == "recovering",
        )
        .values(
            source_gate="normal",
            blocked_id=None,
            blocked_kind=None,
            safe_error=None,
            blocked_at=None,
            action=None,
            updated_at=func.now(),
        )
    )


def defer(owner, claim, reason, *, source_block=False, retry=False, delay=2):
    """Return True if paused/retrying, False when no automatic retries remain."""
    with owner.engine.begin() as conn:
        row = guard_task(conn, claim, owner.token, owner.pid, lock=True, check_cancel=False)
        table = table_for(claim["kind"])
        if source_block:
            conn.execute(
                update(source_runtime)
                .where(source_runtime.c.id == 1)
                .values(
                    source_gate="needs_operator",
                    blocked_kind=claim["kind"],
                    blocked_id=claim["id"],
                    safe_error=reason,
                    blocked_at=func.now(),
                    updated_at=func.now(),
                )
            )
        if claim["kind"] == "job" and row["cancel_requested"]:
            conn.execute(
                update(table)
                .where(id_column(claim["kind"]) == claim["id"])
                .values(
                    status="cancelled",
                    completed_at=func.now(),
                    owner_token=None,
                    lease_until=None,
                    action=None,
                )
            )
            return True
        values = {"safe_error": reason, "owner_token": None, "lease_until": None}
        if source_block:
            values.update(status="blocked", action=None)
        elif retry:
            if row["retry_count"] >= 2 or row["requests"] >= row["max_requests"]:
                return False
            delay = (60, 300)[row["retry_count"]]
            values.update(
                status="waiting_source",
                retry_count=row["retry_count"] + 1,
                not_before=datetime.now(UTC) + timedelta(seconds=delay),
            )
        elif reason in {"cooldown_active", "source_busy", "task_busy"}:
            values.update(
                status="waiting_source", not_before=datetime.now(UTC) + timedelta(seconds=delay)
            )
        else:
            return False
        conn.execute(update(table).where(id_column(claim["kind"]) == claim["id"]).values(**values))
        return True


def fail(owner, claim, reason):
    with owner.engine.begin() as conn:
        row = guard_task(conn, claim, owner.token, owner.pid, lock=True, check_cancel=False)
        table = table_for(claim["kind"])
        status = "cancelled" if claim["kind"] == "job" and row["cancel_requested"] else "failed"
        conn.execute(
            update(table)
            .where(id_column(claim["kind"]) == claim["id"])
            .values(
                status=status,
                safe_error=reason,
                completed_at=func.now(),
                owner_token=None,
                lease_until=None,
                action=None,
            )
        )
        # A failed recovery never opens the source gate.
        conn.execute(
            update(source_runtime)
            .where(
                source_runtime.c.id == 1,
                source_runtime.c.blocked_kind == claim["kind"],
                source_runtime.c.blocked_id == claim["id"],
                source_runtime.c.source_gate == "recovering",
            )
            .values(source_gate="needs_operator", action=None, updated_at=func.now())
        )
