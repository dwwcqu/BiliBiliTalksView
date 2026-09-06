"""Atomic current pointer publication and bounded whole-batch reading."""
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from .contract import ContractError
from .export import write_json
from .validation import read_json, read_lines, safe_path, validate_batch


@contextmanager
def exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ContractError("busy") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def publish_batch(batch_dir: Path, container: Path) -> Path:
    manifest = validate_batch(batch_dir)
    container.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(container / ".publish.lock"):
        current = container / "current.json"
        old = read_json(current, "current") if current.exists() else None
        if old:
            old_batch = safe_path(container, old["batch_path"])
            old_manifest = validate_batch(old_batch)
            if manifest["video_id"] != old_manifest["video_id"]:
                raise ContractError("wrong_video_container")
            if old_manifest["coverage"]["status"] == "verified" and manifest["coverage"]["status"] == "partial":
                raise ContractError("partial_not_published")
        relative = "batches/" + manifest["export_id"]
        target = safe_path(container, relative)
        target.parent.mkdir(exist_ok=True)
        if target.exists():
            if batch_dir.resolve() != target or (old and old["batch_path"] == relative):
                raise ContractError("batch_already_exists")
            # Recover a validated final directory left before pointer publication.
        else:
            staging = safe_path(container, "batches/.staging-" + manifest["export_id"])
            if staging.exists():
                raise ContractError("staging_exists")
            shutil.copytree(batch_dir, staging)
            validate_batch(staging)
            for attempt in range(5):
                if target.exists():
                    raise ContractError("batch_already_exists")
                try:
                    staging.rename(target)
                    break
                except OSError as exc:
                    if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 4:
                        raise
                    time.sleep((0.1, 0.3, 0.8, 1.5)[attempt])
        pointer = {key: manifest[key] for key in ("schema_version", "video_id", "export_id")}
        pointer["batch_path"] = relative
        pending = container / (".current-" + manifest["export_id"] + ".json")
        write_json(pending, pointer)
        os.replace(pending, current)
        if old and old["batch_path"] != relative:
            old_path = safe_path(container, old["batch_path"])
            try:
                shutil.rmtree(old_path)
            except OSError:
                pass  # Referenced batch is safe; cleanup can be retried on the next publication.
        return target


def read_current(container: Path) -> tuple[dict, list[dict]]:
    pointer_path = container / "current.json"
    try:
        pointer = read_json(pointer_path, "current")
    except FileNotFoundError as exc:
        raise ContractError("not_published") from exc
    except (OSError, ValueError, UnicodeError) as exc:
        raise ContractError("invalid_export") from exc
    for attempt in range(3):
        try:
            batch = safe_path(container, pointer["batch_path"])
            manifest = validate_batch(batch)
            if any(manifest[k] != pointer[k] for k in ("schema_version", "video_id", "export_id")):
                raise ContractError("identity_mismatch")
            rows = []
            for entry in manifest["threads"]:
                thread = read_json(safe_path(batch, entry["path"]), "thread")
                rows.extend(read_lines(safe_path(batch, thread["comments_path"])))
            return manifest, rows
        except (OSError, ValueError, UnicodeError) as exc:
            try:
                latest = read_json(pointer_path, "current")
            except FileNotFoundError as missing:
                raise ContractError("export_unavailable") from missing
            except (OSError, ValueError, UnicodeError) as invalid:
                raise ContractError("invalid_export") from invalid
            if latest["export_id"] == pointer["export_id"]:
                raise ContractError("invalid_export") from exc
            if attempt == 2:
                raise ContractError("batch_changed") from exc
            pointer = latest
    raise ContractError("batch_changed")
