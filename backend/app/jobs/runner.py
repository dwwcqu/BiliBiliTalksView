"""Serial worker: all remote activity is fenced and all results use atomic handoff."""

import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.comment_export.access_control import AccessControlError
from app.comment_export.availability import commit_unavailable
from app.comment_export.checkpoint import Checkpoint, read_checkpoint, read_control_snapshot
from app.comment_export.cli import _client
from app.comment_export.collector import collect
from app.comment_export.contract import ContractError
from app.comment_export.diagnostics import is_reply_unavailable
from app.comment_export.publication import exclusive_lock
from app.comment_export.recovery import _probe_locked, _save_failure
from app.comment_export.request_budget import task_budget
from app.comment_export.source import CollectionStopped, resolve_video
from app.storage.errors import StorageError

from . import dispatch, repository
from .errors import JobError
from .ownership import TaskLease, WorkerOwner, assert_owner, guard_task, id_column, table_for
from .schema import collection_jobs, source_runtime
from .work import hour_label, prepare_job, save_result

SOURCE_BLOCKS = {"authentication_required", "rate_limited", "access_restricted", "request_rejected"}


def safe_reason(exc):
    reason = getattr(
        exc,
        "reason",
        getattr(exc, "code", str(exc) if isinstance(exc, ContractError) else "operation_failed"),
    )
    return (
        reason
        if isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]*", reason)
        else "operation_failed"
    )


class Worker:
    def __init__(self, engine, data_root=Path("data/jobs"), client_factory=_client):
        self.engine = engine
        self.data_root = Path(data_root).resolve()
        self.client_factory = client_factory
        self.owner = WorkerOwner(engine)

    def __enter__(self):
        self.owner.__enter__()
        return self

    def __exit__(self, *args):
        return self.owner.__exit__(*args)

    def workspace(self, identity):
        if str(UUID(identity)) != identity:
            raise JobError("invalid_job_id")
        path = self.data_root / identity
        if path.is_symlink() or not path.resolve().is_relative_to(self.data_root):
            raise JobError("invalid_work_directory")
        return path

    def run_once(self):
        claim = dispatch.claim_next(self.owner)
        if claim is None:
            return {"status": "idle"}
        if claim["kind"] == "revalidate":
            return self._revalidate(claim)
        root = self.workspace(claim["id"])
        try:
            with (
                TaskLease(self.owner, claim) as lease,
                exclusive_lock(root / ".task.lock"),
                self.client_factory() as client,
            ):
                client.collection_guard = lease.check
                client.event_hooks["request"].append(lease.before_request)
                if claim["kind"] == "resolution":
                    self._resolve(claim, root, client)
                else:
                    self._collect(claim, root, client, lease)
        except (
            CollectionStopped,
            AccessControlError,
            JobError,
            StorageError,
            ContractError,
            OSError,
        ) as exc:
            reason = safe_reason(exc)
            if isinstance(exc, ContractError) and str(exc) == "busy":
                reason = "task_busy"
            try:
                if reason in {"lease_lost", "worker_stopped"}:
                    return {"status": "interrupted", "error": reason}
                if not self._pause(
                    claim, reason, getattr(exc, "detail", None), client=locals().get("client")
                ):
                    dispatch.fail(self.owner, claim, reason)
            except (JobError, SQLAlchemyError):
                return {"status": "interrupted", "error": "lease_lost"}
        except SQLAlchemyError:
            return {"status": "interrupted", "error": "database_unavailable"}
        with self.engine.connect() as conn:
            result = (
                repository.get_job(conn, claim["id"])
                if claim["kind"] == "job"
                else repository.get_request(conn, claim["id"])
            )
        if result["status"] in {"succeeded", "ready"}:
            from .maintenance import remove_workspace

            remove_workspace(self.data_root, claim["id"])
        return {
            "status": result["status"],
            "job_id" if claim["kind"] == "job" else "request_id": claim["id"],
        }

    def _pause(self, claim, reason, detail=None, *, client=None):
        category = getattr(detail, "category", None)
        source_block = category in SOURCE_BLOCKS or reason in {
            "access_restricted",
            "blocked_requires_revalidation",
        }
        retry = reason == "network_error" or category in {"network_error", "source_unavailable"}
        delay = 2
        if reason == "cooldown_active" and hasattr(client, "access_policy"):
            until = client.access_policy.read_status().get("cooldown_until")
            delay = max(2, (until or 0) - datetime.now(UTC).timestamp())
        return dispatch.defer(
            self.owner, claim, reason, source_block=source_block, retry=retry, delay=delay
        )

    def _resolve(self, claim, root, client):
        work = root / "resolve"
        with exclusive_lock(work / ".collect.lock"):
            cp = Checkpoint(work / "work.sqlite3")
            try:
                progress = cp.get_progress()
                progress.update(
                    input_url=claim["normalized_url"],
                    max_requests=6,
                    requests=max(progress.get("requests", 0), claim["requests"]),
                )
                cp.set_progress(progress)
            finally:
                cp.close()
            if progress.get("blocked"):
                if claim["action"] != "recover":
                    raise CollectionStopped("blocked_requires_revalidation")
                _probe_locked(work, client, False)
            cp = Checkpoint(work / "work.sqlite3")
            try:
                with task_budget(cp, client, 6):
                    resolved = resolve_video(claim["normalized_url"], client)
            except (CollectionStopped, ContractError) as exc:
                _save_failure(cp, client, exc)
                progress = cp.get_progress()
                if safe_reason(exc) == "network_error" or getattr(
                    getattr(exc, "detail", None), "category", None
                ) in {"network_error", "source_unavailable"}:
                    progress["blocked"] = False
                    cp.set_progress(progress)
                raise
            finally:
                cp.close()
            client.collection_guard()
            with self.engine.connect() as conn:
                repository.resolve_request(
                    conn,
                    claim["id"],
                    claim["owner_token"],
                    resolved,
                    commit_guard=lambda connection: guard_task(
                        connection, claim, self.owner.token, self.owner.pid, lock=True
                    ),
                )

    def _collect(self, claim, root, client, lease):
        from .maintenance import cleanup_previous

        cleanup_previous(self.engine, self.data_root, claim["video_id"], claim["id"])
        with exclusive_lock(root / "collection" / ".collect.lock"):
            collection, baseline_version = prepare_job(self.owner, claim, root)
            lease.work_dir = collection
            client.collection_context = {"hour_bucket": hour_label(claim["hour_bucket"])}
            snapshot = read_control_snapshot(collection)
            proof = None
            if snapshot["progress"].get("blocked"):
                if claim["action"] != "recover":
                    raise CollectionStopped("blocked_requires_revalidation")
                try:
                    _, proof = _probe_locked(collection, client, False)
                except CollectionStopped as exc:
                    if not is_reply_unavailable(exc.detail):
                        raise
                    cp = Checkpoint(collection / "work.sqlite3")
                    try:
                        commit_unavailable(cp, exc.detail)
                    finally:
                        cp.close()
            collect(
                claim["input_url"],
                collection,
                client,
                claim["max_requests"],
                resume=True,
                recovery_proof=proof,
                requested_mode=claim["requested_mode"],
            )
            rows, metadata, progress = read_checkpoint(collection)
            if not progress.get("finished"):
                reason = progress.get("stopped_reason") or "collection_incomplete"
                from app.comment_export.diagnostics import FailureDetail

                detail = FailureDetail(**progress["failure"]) if progress.get("failure") else None
                if self._pause(claim, reason, detail, client=client):
                    return
                if metadata.get("video_id") is None:
                    dispatch.fail(self.owner, claim, reason)
                    return
            lease.check()
            with self.engine.begin() as conn:
                current = guard_task(conn, claim, self.owner.token, self.owner.pid, lock=True)
                conn.execute(
                    update(collection_jobs)
                    .where(collection_jobs.c.job_id == claim["id"])
                    .values(
                        phase="import",
                        requests=max(current["requests"], progress.get("requests", 0)),
                        safe_error=None
                        if progress.get("finished")
                        else progress.get("stopped_reason"),
                        progress={
                            "phase": "import",
                            "comments": len(rows),
                            "threads": len(metadata.get("_threads", {})),
                            "updated_at": datetime.now(UTC).isoformat(),
                        },
                    )
                )
            save_result(self.owner, claim, collection, root, baseline_version)

    def _revalidate(self, claim):
        kind, identity = claim["target_kind"], claim["id"]
        if kind not in {"job", "resolution"} or identity is None:
            raise JobError("revalidation_target_missing")
        root = self.workspace(identity)
        work = root / ("collection" if kind == "job" else "resolve")
        with (
            exclusive_lock(root / ".task.lock"),
            exclusive_lock(work / ".collect.lock"),
            self.client_factory() as client,
        ):

            def check():
                with self.engine.begin() as conn:
                    assert_owner(conn, self.owner.token, self.owner.pid)

            def debit(request):
                check()
                with self.engine.begin() as conn:
                    table = table_for(kind)
                    row = (
                        conn.execute(
                            select(table).where(id_column(kind) == identity).with_for_update()
                        )
                        .mappings()
                        .one()
                    )
                    if row["requests"] >= row["max_requests"]:
                        raise CollectionStopped("budget_exhausted")
                    conn.execute(
                        update(table)
                        .where(id_column(kind) == identity)
                        .values(requests=table.c.requests + 1)
                    )

            client.collection_guard = check
            client.event_hooks["request"].append(debit)
            success, error = False, None
            try:
                _probe_locked(work, client, False)
                success = True
            except (CollectionStopped, AccessControlError, ContractError) as exc:
                success = is_reply_unavailable(getattr(exc, "detail", None))
                error = safe_reason(exc)
            with self.engine.begin() as conn:
                assert_owner(conn, self.owner.token, self.owner.pid, lock=True)
                conn.execute(
                    update(source_runtime)
                    .where(source_runtime.c.id == 1)
                    .values(
                        source_gate="normal" if success else "needs_operator",
                        action=None,
                        blocked_id=None if success else identity,
                        blocked_kind=None if success else kind,
                        safe_error=None if success else error,
                        updated_at=func.now(),
                    )
                )
            return {"status": "source_ready" if success else "blocked", "error": error}
