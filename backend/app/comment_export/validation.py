"""Validate exported files without trusting names, indexes, or duplicate copies."""
from pathlib import Path

from .contract import ContractError, parse_json, validate_record
from .dataset_validation import validate_documents


def safe_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or "\\" in relative or ":" in relative:
        raise ContractError("unsafe_path")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ContractError("unsafe_path")
    result = (root / path).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ContractError("unsafe_path")
    return result


def read_json(path: Path, kind: str) -> dict:
    value = parse_json(path.read_text(encoding="utf-8"))
    validate_record(kind, value)
    return value


def _parse_lines(path: Path) -> list[dict]:
    data = path.read_bytes()
    if data and not data.endswith(b"\n") or b"\r" in data or data.startswith(b"\xef\xbb\xbf"):
        raise ContractError("invalid_jsonl_encoding")
    rows = []
    for line in data.decode("utf-8").split("\n")[:-1]:
        value = parse_json(line)
        rows.append(value)
    return rows


def read_lines(path: Path, kind: str = "comment") -> list[dict]:
    rows = _parse_lines(path)
    for value in rows:
        validate_record(kind, value)
    return rows


def load_validated_documents(batch_dir: Path) -> dict:
    paths = list(batch_dir.rglob("*"))
    documents = {path.relative_to(batch_dir).as_posix(): None
                 for path in paths if path.is_file()}

    def load_json(relative):
        value = parse_json(safe_path(batch_dir, relative).read_text(encoding="utf-8"))
        documents[relative] = value
        return value

    manifest = load_json("manifest.json")
    documents["README.md"] = ("" if safe_path(batch_dir, "README.md").read_bytes() == b""
                              else "nonempty")
    # Discover declared roles without coercing malformed values; shared validation
    # remains the only schema pass and rejects incomplete or invalid records.
    if isinstance(manifest, dict):
        for group in ("threads", "users"):
            entries = manifest.get(group)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                    continue
                info = load_json(entry["path"])
                if isinstance(info, dict) and isinstance(info.get("comments_path"), str):
                    relative = info["comments_path"]
                    documents[relative] = _parse_lines(safe_path(batch_dir, relative))
        relative = manifest.get("unclassified_path")
        if isinstance(relative, str):
            documents[relative] = _parse_lines(safe_path(batch_dir, relative))
    actual = {path.relative_to(batch_dir).as_posix() for path in paths if path.is_file()}
    if set(documents) != actual:
        raise ContractError("unindexed_files")
    manifest = validate_documents(documents)
    if manifest["schema_version"].startswith("2."):
        import re
        for path in paths:
            pattern = r"[a-z0-9_-]+" if path.is_dir() else r"[A-Za-z0-9_.-]+"
            if not re.fullmatch(pattern, path.name):
                raise ContractError("nonportable_export_path")
    return documents


def validate_batch(batch_dir: Path) -> dict:
    return load_validated_documents(batch_dir)["manifest.json"]
