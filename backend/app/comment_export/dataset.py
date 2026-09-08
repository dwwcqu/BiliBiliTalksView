"""Validated logical documents held in a private, immutable snapshot."""

import hashlib
import json
from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType

from .contract import ContractError, validate_record
from .dataset_validation import _json_value, validate_documents


def _encode(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def _freeze(value: object, records: dict[bytes, object]) -> object:
    if isinstance(value, dict):
        # Copies in the two projections may compare equal while having different
        # JSON numbers. Only identical canonical encodings share a record body.
        try:
            key = _encode(value) if "comment_id" in value and "content" in value else None
        except TypeError:
            # Analysis snapshots retain Decimal values; skip the legacy JSON cache.
            key = None
        if key is not None and key in records:
            return records[key]
        frozen = MappingProxyType({k: _freeze(v, records) for k, v in value.items()})
        if key is not None:
            records[key] = frozen
        return frozen
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, records) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _digest_value(path: str, value: object, unclassified_path: str | None):
    """Reproduce the legacy suffix-based parser without changing logical roles."""
    if PurePosixPath(path).suffix == ".jsonl":
        rows = value if isinstance(value, list) else [value]
        # Ordinary role validation already checked these records. The historical
        # digest imposed an additional schema only when path and role disagree.
        if not isinstance(value, list) or (
            (path == "unclassified.jsonl") != (path == unclassified_path)
        ):
            kind = "unclassified" if path == "unclassified.jsonl" else "comment"
            for row in rows:
                validate_record(kind, row)
        return rows
    if isinstance(value, list):
        # Only one JSONL object also forms a valid complete JSON document.
        if len(value) != 1:
            raise ContractError("invalid_json")
        return value[0]
    return value


def _digest(documents: Mapping[str, object]) -> str:
    digest = hashlib.sha256()
    digest.update(b"[")
    for index, path in enumerate(sorted(documents)):
        if index:
            digest.update(b",")
        value = _digest_value(path, documents[path], documents["manifest.json"]["unclassified_path"])
        digest.update(_encode([path, value]))
    digest.update(b"]")
    return digest.hexdigest()


@dataclass(frozen=True, init=False, repr=False)
class ValidatedDataset:
    _documents: Mapping[str, object]
    _evidence: object
    _digest: str

    @classmethod
    def from_documents(cls, documents: Mapping[str, object], *, evidence=None):
        snapshot = deepcopy(dict(documents))
        validate_documents(snapshot)
        return cls._from_validated_documents(snapshot, evidence=evidence)

    @classmethod
    def _from_validated_documents(cls, documents, *, evidence=None, digest: str | None = None):
        """Internal file adapter entry after the shared and physical checks."""
        evidence = deepcopy(evidence)
        _json_value(evidence)
        instance = object.__new__(cls)
        records = {}
        object.__setattr__(instance, "_documents", _freeze(documents, records))
        object.__setattr__(instance, "_evidence", _freeze(evidence, records))
        object.__setattr__(instance, "_digest", _digest(documents) if digest is None else digest)
        return instance

    @property
    def manifest(self) -> dict:
        return self.read_document("manifest.json")

    @property
    def evidence(self):
        return _thaw(self._evidence)

    @property
    def digest(self) -> str:
        return self._digest

    def read_document(self, path: str):
        return _thaw(self._documents[path])

    def read_lines(self, path: str) -> list[dict]:
        value = self.read_document(path)
        if not isinstance(value, list):
            raise TypeError("not_line_document")
        return value

    def iter_documents(self) -> Iterator[tuple[str, object]]:
        for path in sorted(self._documents):
            yield path, self.read_document(path)

    def iter_comments(self, root_id: str | None = None) -> Iterator[dict]:
        manifest = self._documents["manifest.json"]
        for entry in manifest["threads"]:
            if root_id is None or entry["root_id"] == root_id:
                info = self._documents[entry["path"]]
                for row in self._documents[info["comments_path"]]:
                    yield _thaw(row)
