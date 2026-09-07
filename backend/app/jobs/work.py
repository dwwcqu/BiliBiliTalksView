"""Frozen per-job workspaces and the C1 database handoff boundary."""

from datetime import timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from sqlalchemy import update

from app.comment_export.checkpoint import Checkpoint, read_checkpoint
from app.comment_export.collector import now
from app.comment_export.export import build_dataset
from app.comment_export.incremental import prepare_refresh
from app.comment_export.zero_schedule import decide_policy
from app.storage.baseline import freeze_baseline
from app.storage.handoff import materialize, prepare_dataset_handoff

from .dispatch import complete_job
from .errors import JobError
from .ownership import guard_task
from .schema import collection_jobs


def hour_label(stamp):
    return stamp.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:00:00+08:00")


def prepare_job(owner, claim, root):
    collection = root / "collection"
    database = collection / "work.sqlite3"
    if not database.exists():
        baseline_dir = root / "baseline"
        if (baseline_dir / "work.sqlite3").exists():
            rows, metadata, progress = read_checkpoint(baseline_dir)
            baseline = progress["database_baseline"]
        elif claim["baseline_version"] is None:
            with owner.engine.connect() as conn:
                baseline = freeze_baseline(conn, claim["video_id"], baseline_dir)
            if baseline["state_id"] is not None:
                rows, metadata, _ = read_checkpoint(baseline_dir)
            else:
                rows, metadata = [], {}
        else:
            if claim["base_state_id"] is not None:
                raise JobError("baseline_missing")
            baseline = {"cache_version": claim["baseline_version"], "state_id": None}
            rows, metadata = [], {}
        if metadata:
            rows, metadata, progress = prepare_refresh(
                rows, metadata, {}, claim["requested_mode"], now(), 24,
                work_id=claim["id"],
                baseline_binding=f"{baseline['cache_version']}:{baseline['state_id']}"
            )
            metadata["hour_bucket"] = hour_label(claim["hour_bucket"])
            metadata["input_url"] = claim["input_url"]
            progress["metadata"] = metadata
            effective = metadata["_refresh"]["mode"]
        else:
            progress = {
                "refresh_request": {"version": 1, "requested_mode": claim["requested_mode"]},
                "zero_policy_snapshot": decide_policy(claim["requested_mode"], now(), 24,
                    None, work_id=claim["id"], baseline_binding=None),
                "zero_baseline_root_ids": [],
            }
            effective = "full"
        progress.update(
            job_id=claim["id"],
            baseline_version=baseline["cache_version"],
            base_state_id=baseline["state_id"],
            effective_mode=effective,
            input_url=claim["input_url"],
            requests=claim["requests"],
            max_requests=claim["max_requests"],
        )
        collection.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".seed-", dir=root) as temporary:
            staged = Path(temporary) / "work.sqlite3"
            cp = Checkpoint(staged)
            try:
                cp.commit_page("job:seed", rows, progress)
            finally:
                cp.close()
            if database.exists():
                raise JobError("checkpoint_exists")
            staged.rename(database)
    _, _, progress = read_checkpoint(collection)
    if progress.get("job_id") != claim["id"]:
        raise JobError("checkpoint_job_mismatch")
    with owner.engine.begin() as conn:
        current = guard_task(conn, claim, owner.token, owner.pid, lock=True)
        if (
            current["baseline_version"] is not None
            and current["baseline_version"] != progress["baseline_version"]
        ):
            raise JobError("baseline_changed")
        conn.execute(
            update(collection_jobs)
            .where(collection_jobs.c.job_id == claim["id"])
            .values(
                baseline_version=progress["baseline_version"],
                base_state_id=progress["base_state_id"],
                effective_mode=progress["effective_mode"],
                phase="main",
            )
        )
    cp = Checkpoint(database)
    try:
        progress = cp.get_progress()
        progress["requests"] = max(progress.get("requests", 0), current["requests"])
        progress["max_requests"] = min(
            progress.get("max_requests", current["max_requests"]), current["max_requests"]
        )
        cp.set_progress(progress)
    finally:
        cp.close()
    return collection, progress["baseline_version"]


def save_result(owner, claim, collection, root, baseline_version):
    rows, metadata, _progress = read_checkpoint(collection)
    if metadata.get("video_id") != claim["video_id"]:
        raise JobError("video_identity_mismatch")
    exported = dict(metadata, schema_version="2.0.0", export_id=str(uuid4()), exported_at=now())
    dataset_rows = [dict(row, schema_version="2.0.0", export_id=exported["export_id"]) for row in rows]
    dataset = build_dataset(dataset_rows, exported)
    handoff = prepare_dataset_handoff(dataset, claim["id"], baseline_version)
    with owner.engine.connect() as conn:
        return materialize(
            conn,
            dataset,
            handoff,
            lambda connection, result: complete_job(connection, owner, claim, result),
        )
