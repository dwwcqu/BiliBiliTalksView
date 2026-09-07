"""Freeze validated inputs before starting database side effects."""

import hashlib
import json
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from app.comment_export.contract import parse_json
from app.comment_export.dataset import ValidatedDataset
from app.comment_export.validation import (
    load_validated_documents,
    read_json,
    read_lines,
    safe_path,
)

from .errors import StorageError


@dataclass(frozen=True)
class FrozenBatch:
    directory: Path
    dataset: ValidatedDataset

    @property
    def manifest(self) -> dict:
        return self.dataset.manifest

    @property
    def digest(self) -> str:
        return self.dataset.digest

    @property
    def evidence(self):
        return self.dataset.evidence

    def read_document(self, path: str):
        return self.dataset.read_document(path)

    def read_lines(self, path: str) -> list[dict]:
        return self.dataset.read_lines(path)

    def iter_comments(self, root_id: str | None = None):
        return self.dataset.iter_comments(root_id)

    def iter_documents(self):
        return self.dataset.iter_documents()


def canonical_digest(directory: Path) -> str:
    entries = []
    for path in sorted(
        directory.rglob("*"), key=lambda path: path.relative_to(directory).as_posix()
    ):
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        if relative == "README.md":
            value = ""
        elif path.suffix == ".jsonl":
            value = read_lines(
                path, "unclassified" if relative == "unclassified.jsonl" else "comment"
            )
        else:
            value = parse_json(path.read_text(encoding="utf-8"))
        entries.append([relative, value])
    encoded = json.dumps(
        entries, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def freeze_batch(input_path: Path, work_root: Path):
    input_path = input_path.resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    is_pointer = input_path.name == "current.json" and not input_path.is_dir()
    pointer = None
    try:
        if is_pointer:
            pointer = read_json(input_path, "current")
    except (OSError, ValueError) as exc:
        raise StorageError("invalid_export") from exc
    for attempt in range(3):
        with TemporaryDirectory(prefix="freeze-", dir=work_root.resolve()) as temporary:
            target = Path(temporary) / "batch"
            try:
                source = (
                    safe_path(input_path.parent, pointer["batch_path"]) if pointer else input_path
                )
                if work_root.resolve().is_relative_to(source):
                    raise StorageError("unsafe_work_directory")
                shutil.copytree(source, target, symlinks=True)
                documents = load_validated_documents(target)
                manifest = documents["manifest.json"]
                if pointer and any(
                    manifest[k] != pointer[k] for k in ("schema_version", "video_id", "export_id")
                ):
                    raise StorageError("invalid_export")
                # Legacy digest follows suffixes; validation follows declared roles.
                # A single JSONL row with a non-jsonl suffix hashes as an object.
                for relative, value in documents.items():
                    if relative == "README.md":
                        continue
                    path = target / relative
                    if path.suffix == ".jsonl" and not isinstance(value, list):
                        read_lines(
                            path, "unclassified" if relative == "unclassified.jsonl" else "comment"
                        )
                    elif path.suffix != ".jsonl" and isinstance(value, list):
                        parse_json(path.read_text(encoding="utf-8"))
                dataset = ValidatedDataset._from_validated_documents(documents)
                frozen = FrozenBatch(target, dataset)
            except (OSError, ValueError) as exc:
                if not pointer:
                    raise StorageError("invalid_export") from exc
                try:
                    latest = read_json(input_path, "current")
                except (OSError, ValueError) as missing:
                    raise StorageError("export_unavailable") from missing
                if latest["export_id"] == pointer["export_id"]:
                    raise StorageError("invalid_export") from exc
                if attempt == 2:
                    raise StorageError("batch_changed") from exc
                pointer = latest
                continue
            yield frozen
            return
