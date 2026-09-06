"""Bounded cleanup restricted to registered UUID workspaces and expired metadata."""

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, select, update

from app.comment_export.publication import exclusive_lock

from .schema import collection_jobs, resolution_requests, source_runtime

TERMINAL = ("succeeded", "partial", "failed", "cancelled")


def remove_workspace(root, identity):
    if str(UUID(identity)) != identity:
        raise ValueError("invalid_job_id")
    root = Path(root).resolve()
    path = root / identity
    if not path.exists():
        return
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("invalid_work_directory")
    with ExitStack() as stack:
        stack.enter_context(exclusive_lock(path / ".task.lock"))
        for name in ("collection", "resolve", "baseline"):
            if (path / name).exists():
                stack.enter_context(exclusive_lock(path / name / ".collect.lock"))
        for item in path.rglob("*"):
            if item.is_symlink() or not item.resolve().is_relative_to(path.resolve()):
                continue
            if item.is_file() and item.name not in {".collect.lock", ".task.lock"}:
                item.unlink()
        for item in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if (
                item.is_dir()
                and not item.is_symlink()
                and item.resolve().is_relative_to(path.resolve())
            ):
                try:
                    item.rmdir()
                except OSError:
                    pass


def cleanup_previous(engine, root, video_id, current_id):
    with engine.begin() as conn:
        held = conn.execute(
            select(source_runtime.c.blocked_id).where(source_runtime.c.id == 1)
        ).scalar_one()
        rows = (
            conn.execute(
                select(collection_jobs)
                .where(
                    collection_jobs.c.video_id == video_id,
                    collection_jobs.c.job_id != current_id,
                    collection_jobs.c.status.in_(TERMINAL),
                )
                .with_for_update()
            )
            .mappings()
            .all()
        )
        ids = []
        for row in rows:
            if row["job_id"] == held:
                continue
            ids.append(row["job_id"])
            conn.execute(
                update(collection_jobs)
                .where(collection_jobs.c.job_id == row["job_id"])
                .values(progress={**row["progress"], "work_expired": True})
            )
    for identity in ids:
        try:
            remove_workspace(root, identity)
        except (OSError, ValueError):
            pass


def purge_metadata(engine, data_root=Path("data/jobs"), now=None):
    cutoff = (now or datetime.now(UTC)) - timedelta(days=7)
    removed = {"requests": 0, "jobs": 0}
    with engine.begin() as conn:
        held = conn.execute(
            select(source_runtime.c.blocked_id).where(source_runtime.c.id == 1)
        ).scalar_one()
        requests = (
            select(resolution_requests)
            .where(
                resolution_requests.c.status.in_(["ready", "failed"]),
                resolution_requests.c.completed_at < cutoff,
            )
            .with_for_update()
        )
        for row in conn.execute(requests).mappings():
            identity = row["request_id"]
            if identity == held:
                continue
            try:
                remove_workspace(data_root, identity)
            except (OSError, ValueError):
                continue
            conn.execute(
                delete(resolution_requests).where(resolution_requests.c.request_id == identity)
            )
            removed["requests"] += 1
        referenced = (
            select(resolution_requests.c.request_id)
            .where(resolution_requests.c.job_id == collection_jobs.c.job_id)
            .exists()
        )
        jobs = (
            select(collection_jobs)
            .where(
                collection_jobs.c.status.in_(TERMINAL),
                collection_jobs.c.completed_at < cutoff,
                collection_jobs.c.result_state_id.is_(None),
                ~referenced,
            )
            .with_for_update()
        )
        for row in conn.execute(jobs).mappings():
            identity = row["job_id"]
            if identity == held:
                continue
            try:
                remove_workspace(data_root, identity)
            except (OSError, ValueError):
                continue
            conn.execute(delete(collection_jobs).where(collection_jobs.c.job_id == identity))
            removed["jobs"] += 1
    return removed
