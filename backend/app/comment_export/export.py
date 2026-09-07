"""Build both projections from one immutable normalized collection."""
import json
from pathlib import Path

from .dataset import ValidatedDataset
from .dataset_builder import build_dataset
from .layout import (
    LATEST_SCHEMA_VERSION,
    nickname,
    order,
    thread_directory,
    user_directory,
    user_folder,
)

__all__ = [
    "LATEST_SCHEMA_VERSION",
    "build_batch",
    "nickname",
    "order",
    "thread_directory",
    "user_directory",
    "user_folder",
    "write_dataset",
    "write_json",
]


def write_json(path: Path, value, lines: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", errors="backslashreplace", newline="\n") as stream:
        if lines:
            for row in value:
                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        else:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n")


def write_dataset(dataset: ValidatedDataset, destination: Path) -> Path:
    """Materialize a validated snapshot only when an export was requested."""
    manifest = dataset.manifest
    destination.mkdir(parents=True, exist_ok=False)
    for path, value in dataset.iter_documents():
        if path == "README.md":
            (destination / path).write_bytes(b"")
        elif Path(path).suffix == ".jsonl" and not isinstance(value, list):
            # A metadata object with a JSONL suffix must satisfy both readers.
            write_json(destination / path, [value], lines=True)
        else:
            write_json(destination / path, value, lines=isinstance(value, list))
    # Keep the traditional empty directories without shadowing legal v1 files.
    for directory in (thread_directory(manifest["schema_version"]),
                      user_directory(manifest["schema_version"])):
        path = destination / directory
        if not path.exists():
            path.mkdir()
    return destination


def build_batch(records: list[dict], manifest: dict, destination: Path) -> Path:
    return write_dataset(build_dataset(records, manifest), destination)
