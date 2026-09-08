"""Validate exported files without trusting names, indexes, or duplicate copies."""
import json
from decimal import Decimal
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


def _parse_document(text: str, *, analysis_readme: bool = False):
    value = parse_json(text)
    # Keep duplicate-key/nonfinite-token rejection, then retain decimal precision.
    return json.loads(text, parse_float=Decimal) if analysis_readme else value


def _parse_lines(path: Path, *, analysis_readme: bool = False) -> list[dict]:
    data = path.read_bytes()
    if data and not data.endswith(b"\n") or b"\r" in data or data.startswith(b"\xef\xbb\xbf"):
        raise ContractError("invalid_jsonl_encoding")
    rows = []
    for line in data.decode("utf-8").split("\n")[:-1]:
        value = _parse_document(line, analysis_readme=analysis_readme)
        rows.append(value)
    return rows


def read_lines(path: Path, kind: str = "comment") -> list[dict]:
    rows = _parse_lines(path)
    for value in rows:
        validate_record(kind, value)
    return rows


def load_validated_documents(batch_dir: Path, *, analysis_readme: bool = False) -> dict:
    paths = list(batch_dir.rglob("*"))
    if analysis_readme:
        for path in (batch_dir, *paths):
            if path.is_symlink() or path.is_junction():
                raise ContractError("unsafe_path")
            if path.is_file() and path.relative_to(batch_dir).as_posix() != "README.md":
                raw = path.read_bytes()
                raw.decode("utf-8")
                if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
                    raise ContractError("invalid_export_encoding")
    documents = {path.relative_to(batch_dir).as_posix(): None
                 for path in paths if path.is_file()}

    def load_json(relative):
        value = _parse_document(safe_path(batch_dir, relative).read_text(encoding="utf-8"),
                                analysis_readme=analysis_readme)
        documents[relative] = value
        return value

    manifest = load_json("manifest.json")
    readme = safe_path(batch_dir, "README.md").read_bytes()
    documents["README.md"] = (readme.decode("utf-8") if analysis_readme
                              else "" if readme == b"" else "nonempty")
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
                    documents[relative] = _parse_lines(
                        safe_path(batch_dir, relative), analysis_readme=analysis_readme)
        relative = manifest.get("unclassified_path")
        if isinstance(relative, str):
            documents[relative] = _parse_lines(
                safe_path(batch_dir, relative), analysis_readme=analysis_readme)
    actual = {path.relative_to(batch_dir).as_posix() for path in paths if path.is_file()}
    if set(documents) != actual:
        raise ContractError("unindexed_files")
    manifest = validate_documents(documents, analysis_readme=analysis_readme)
    if manifest["schema_version"].startswith("2."):
        import re
        for path in paths:
            pattern = r"[a-z0-9_-]+" if path.is_dir() else r"[A-Za-z0-9_.-]+"
            if not re.fullmatch(pattern, path.name):
                raise ContractError("nonportable_export_path")
    return documents


def validate_batch(batch_dir: Path, *, analysis_readme: bool = False) -> dict:
    return load_validated_documents(batch_dir, analysis_readme=analysis_readme)["manifest.json"]
