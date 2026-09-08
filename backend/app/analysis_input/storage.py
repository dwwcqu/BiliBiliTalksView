"""Atomic immutable input and preparation-run publication."""
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from app.comment_export.contract import parse_json
from app.comment_export.validation import validate_batch

from .digest import ALGORITHM, exact_digest
from .errors import InputPreparationError
from .locking import preparation_lock
from .reader import independent_roots, stage_current, validate_encoding

VERSION = "1.0.0"


def encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def fingerprint(value: object) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def context_data(files: dict[str, Path] | None) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    used = {"readme.md"}
    for name, path in (files or {}).items():
        if (not isinstance(name, str) or not name or name in {".", ".."}
                or any(c in name for c in '/\\:<>"|?*')
                or any(ord(c) < 32 for c in name) or name[-1] in ". "
                or name.casefold() in used
                or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])", name.split(".")[0])):
            raise InputPreparationError("invalid_context")
        used.add(name.casefold())
        try:
            raw = Path(path).read_bytes()
            raw.decode("utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            raise InputPreparationError("invalid_context") from exc
        result[name] = raw
    return result


def safe_child(root: Path, relative: str) -> Path:
    current = root
    for segment in Path(relative).parts:
        if segment in {"..", "."}:
            raise InputPreparationError("invalid_storage_root")
        current = current / segment
        if current.is_symlink() or current.is_junction():
            raise InputPreparationError("invalid_storage_root")
    if not current.resolve().is_relative_to(root.resolve()):
        raise InputPreparationError("invalid_storage_root")
    return current


def require_ignored(root: Path) -> None:
    ancestor = root
    while not ancestor.exists():
        ancestor = ancestor.parent
    try:
        repo = subprocess.run(["git", "-C", str(ancestor), "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, check=False)
        if repo.returncode != 0:
            return  # Explicit external storage need not be in a Git repository.
        ignored = subprocess.run(["git", "-C", repo.stdout.strip(), "check-ignore", "-q",
                                  str(root / ".analysis-write-check")],
                                 capture_output=True, check=False)
        if ignored.returncode != 0:
            raise InputPreparationError("storage_not_ignored")
    except FileNotFoundError:
        # Repository detection still works without invoking git in exported deployments.
        if any((parent / ".git").exists() for parent in [ancestor, *ancestor.parents]):
            raise InputPreparationError("storage_not_ignored") from None


def checked_input(path: Path, digest: str) -> None:
    try:
        validate_encoding(path)
        validate_batch(path, analysis_readme=True)
        actual = exact_digest(path)
    except (OSError, ValueError) as exc:
        raise InputPreparationError("stored_input_invalid") from exc
    if actual != digest:
        raise InputPreparationError("input_conflict")


def existing_run(path: Path, expected: dict, context: dict[str, bytes]) -> None:
    try:
        for name in ("run.json", "context", "intermediate", "users"):
            safe_child(path, name)
        stored = parse_json((path / "run.json").read_text(encoding="utf-8"))
        if not isinstance(stored, dict):
            raise TypeError("invalid_record_type")
        # created_at is historical; all other immutable preparation fields must agree.
        if {k: v for k, v in stored.items() if k != "created_at"} != {
            k: v for k, v in expected.items() if k != "created_at"
        }:
            raise ValueError("record_mismatch")
        datetime.strptime(stored["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        for directory in ("context", "intermediate", "users"):
            if not (path / directory).is_dir():
                raise ValueError("directory_missing")
        if {p.name for p in (path / "context").iterdir()} != set(context):
            raise ValueError("context_set_mismatch")
        for name, raw in context.items():
            if safe_child(path, "context/" + name).read_bytes() != raw:
                raise ValueError("context_mismatch")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise InputPreparationError("stored_run_invalid") from exc


def prepare_input(container: Path, analysis_root: Path, *,
                  context_files: dict[str, Path] | None = None,
                  allow_partial: bool = False) -> dict:
    container, root = independent_roots(Path(container), Path(analysis_root))
    if type(allow_partial) is not bool:
        raise InputPreparationError("invalid_policy")
    context = context_data(context_files)
    require_ignored(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".prepare-", dir=root) as scratch:
            staged = Path(scratch) / "batch"
            manifest = stage_current(container, staged)
            digest = exact_digest(staged)
            context["README.md"] = (staged / "README.md").read_bytes()
            context_hashes = {name: hashlib.sha256(raw).hexdigest()
                              for name, raw in sorted(context.items())}
            request = {"preparation_version": VERSION, "video_id": manifest["video_id"],
                       "export_id": manifest["export_id"], "input_fingerprint": digest,
                       "context_fingerprint": fingerprint(context_hashes),
                       "scope": "whole_video", "allow_partial": allow_partial}
            run_id = "prep-" + fingerprint(request)
            video = safe_child(root, "bilibili-video-" + manifest["source"]["aid"])
            video.mkdir(exist_ok=True)
            with preparation_lock(safe_child(video, ".prepare.lock")):
                for folder in ("inputs", "runs", "sessions"):
                    safe_child(video, folder).mkdir(exist_ok=True)
                target = safe_child(video, "inputs/" + manifest["export_id"])
                if target.exists():
                    checked_input(target, digest)
                else:
                    staged.rename(target)
                run = safe_child(video, "runs/" + run_id)
                status = ("waiting_policy" if manifest["coverage"]["status"] == "partial"
                          and not allow_partial else "no_analyzable_users"
                          if manifest["counts"]["known_users"] == 0 else "ready")
                record = {"schema_version": VERSION, "analysis_run_id": run_id,
                          "request": request, "input_path": target.relative_to(root).as_posix(),
                          "input_digest_algorithm": ALGORITHM,
                          "context_hashes": context_hashes, "coverage": manifest["coverage"],
                          "counts": manifest["counts"], "status": status,
                          "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "model_execution_authorized": False}
                if run.exists():
                    existing_run(run, record, context)
                else:
                    pending = Path(scratch) / "run"
                    pending.mkdir()
                    for folder in ("context", "intermediate", "users"):
                        (pending / folder).mkdir()
                    for name, raw in context.items():
                        (pending / "context" / name).write_bytes(raw)
                    (pending / "run.json").write_bytes(encoded(record) + b"\n")
                    pending.rename(run)
                return {"status": status, "video_id": manifest["video_id"],
                        "export_id": manifest["export_id"], "analysis_run_id": run_id,
                        "input_path": str(target), "run_path": str(run),
                        "coverage": manifest["coverage"], "counts": manifest["counts"],
                        "model_execution_authorized": False}
    except InputPreparationError:
        raise
    except OSError as exc:
        raise InputPreparationError("storage_error") from exc
