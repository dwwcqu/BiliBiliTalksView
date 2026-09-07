"""Freeze an immutable baseline before starting a refresh collection."""

import math
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.storage.errors import StorageError
from app.storage.frozen import freeze_batch

from . import collector
from .checkpoint import Checkpoint, read_checkpoint
from .contract import ContractError
from .incremental import prepare_refresh
from .publication import exclusive_lock, lock_owned
from .validation import read_json, read_lines, safe_path
from .zero_schedule import decide_policy


def _validate_arguments(max_requests, resume, baseline_work, baseline_batch, mode,
                        full_interval_hours):
    if mode not in {"auto", "full"}:
        raise ContractError("invalid_refresh_mode")
    if type(max_requests) is not int or max_requests < 1:
        raise ContractError("invalid_max_requests")
    if (isinstance(full_interval_hours, bool)
            or not isinstance(full_interval_hours, (int, float))
            or not math.isfinite(full_interval_hours)
            or full_interval_hours <= 0):
        raise ContractError("invalid_full_interval")
    if baseline_work is not None and baseline_batch is not None:
        raise ContractError("baseline_conflict")
    if resume and (baseline_work is not None or baseline_batch is not None):
        raise ContractError("resume_with_baseline")


def _batch_baseline(input_path: Path) -> tuple[list[dict], dict, dict]:
    try:
        with (TemporaryDirectory(prefix="refresh-baseline-") as temporary,
              freeze_batch(input_path, Path(temporary)) as frozen):
            manifest = frozen.manifest
            records = []
            threads = {}
            for entry in manifest["threads"]:
                thread = read_json(safe_path(frozen.directory, entry["path"]), "thread")
                records.extend(read_lines(safe_path(
                    frozen.directory, thread["comments_path"])))
                coverage = thread["coverage"]
                threads[entry["root_id"]] = {
                    "pagination_status": coverage["pagination_status"],
                    "count": coverage["source_reported_reply_count"],
                }
            unclassified = []
            if manifest["unclassified_path"]:
                unclassified = read_lines(safe_path(
                    frozen.directory, manifest["unclassified_path"]), "unclassified")
            metadata = {
                key: deepcopy(value)
                for key, value in manifest.items()
                if key not in {
                    "_refresh", "_threads", "_unclassified", "counts", "unclassified_path",
                }
            }
            metadata["_threads"] = threads
            metadata["_unclassified"] = unclassified
            return records, metadata, {}
    except StorageError as exc:
        raise ContractError(str(exc)) from exc


def _work_baseline(path: Path) -> tuple[list[dict], dict, dict]:
    database = path / "work.sqlite3"
    if not database.is_file():
        raise ContractError("task_not_found")
    with exclusive_lock(path / ".collect.lock"):
        return read_checkpoint(path)


def _collect_existing(url, work_dir, client, max_requests, requested_mode=None):
    options = {"resume": True}
    if requested_mode is not None:
        options["requested_mode"] = requested_mode
    result = collector.collect(url, work_dir, client, max_requests, **options)
    _, _, progress = read_checkpoint(work_dir)
    if progress.get("stopped_reason") == "checkpoint_video_mismatch":
        raise ContractError("checkpoint_video_mismatch")
    return result


def refresh(url, work_dir, client, max_requests=12000, resume=False, *,
            baseline_work=None, baseline_batch=None, mode="auto", full_interval_hours=24):
    """Collect a refresh after atomically freezing its baseline and control state."""
    lock = Path(work_dir) / '.collect.lock'
    with nullcontext() if lock_owned(lock) else exclusive_lock(lock):
        return _refresh_locked(url, work_dir, client, max_requests, resume,
                               baseline_work=baseline_work, baseline_batch=baseline_batch,
                               mode=mode, full_interval_hours=full_interval_hours)


def _refresh_locked(url, work_dir, client, max_requests, resume, *, baseline_work,
                    baseline_batch, mode, full_interval_hours):
    _validate_arguments(max_requests, resume, baseline_work, baseline_batch, mode,
                        full_interval_hours)
    work_dir = Path(work_dir)
    target = work_dir.resolve()
    work_database = work_dir / "work.sqlite3"

    if baseline_work is not None and Path(baseline_work).resolve() == target:
        raise ContractError("baseline_is_target")
    if not resume and work_database.exists():
        raise ContractError("checkpoint_exists_use_resume")

    if resume:
        records, metadata, progress = read_checkpoint(work_dir)
        intent = progress.get("refresh_request", {})
        initializing = (not metadata.get("video_id") and isinstance(intent, dict)
                        and intent.get("version") == 1
                        and intent.get("requested_mode") in {"auto", "full"})
        if not initializing and (not metadata.get("video_id") or "_refresh" not in metadata):
            raise ContractError("invalid_refresh_checkpoint")
        frozen_url = progress.get("input_url")
        frozen_budget = progress.get("max_requests")
        if not isinstance(frozen_url, str) or not frozen_url:
            raise ContractError("invalid_refresh_checkpoint")
        if type(frozen_budget) is not int or frozen_budget < 1:
            raise ContractError("invalid_refresh_checkpoint")
        return _collect_existing(frozen_url, work_dir, client, frozen_budget,
                                 intent["requested_mode"] if initializing else None)

    if baseline_work is None and baseline_batch is None:
        checkpoint = Checkpoint(work_database)
        try:
            checkpoint.set_progress({
                "refresh_request": {"version": 1, "requested_mode": mode},
                "zero_policy_snapshot": decide_policy(mode, collector.now(), full_interval_hours,
                    None, work_id=str(uuid4()), baseline_binding=None),
                "zero_baseline_root_ids": [],
                "input_url": url, "max_requests": max_requests, "requests": 0,
            })
        finally:
            checkpoint.close()
        return collector.collect(url, work_dir, client, max_requests, resume=False,
                                 requested_mode=mode)

    if baseline_work is not None:
        records, metadata, progress = _work_baseline(Path(baseline_work))
    else:
        records, metadata, progress = _batch_baseline(Path(baseline_batch))
    if not metadata.get("video_id"):
        raise ContractError("baseline_missing_video_id")

    observed = collector.now()
    try:
        seed_rows, new_metadata, new_progress = prepare_refresh(
            records, metadata, progress, mode, observed, full_interval_hours)
    except ContractError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("invalid_baseline") from exc
    new_metadata["input_url"] = url
    checkpoint = Checkpoint(work_database)
    try:
        checkpoint.commit_page("refresh:baseline", seed_rows, {
            **new_progress,
            "metadata": new_metadata,
            "max_requests": max_requests,
            "input_url": url,
        })
    finally:
        checkpoint.close()
    return _collect_existing(url, work_dir, client, max_requests)
