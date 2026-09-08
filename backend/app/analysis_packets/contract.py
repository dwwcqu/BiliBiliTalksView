"""Validate protocol shapes with a local-only JSON Schema registry."""

import json
import math
import re
from decimal import Decimal
from functools import lru_cache
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource, Unresolvable

from .errors import PacketError

KINDS = frozenset(
    {
        "packet",
        "manifest",
        "task-index-row",
        "target-index-row",
        "group-coverage",
        "member-registry",
        "observation",
    }
)


def _deny_remote(uri):
    raise NoSuchResource(ref=uri)


@lru_cache(maxsize=1)
def _schemas():
    registry = Registry(retrieve=_deny_remote)
    documents = {}
    folder = files("app.analysis_packets").joinpath("schemas")
    for name in sorted(KINDS | {"common"}):
        schema = json.loads(folder.joinpath(name + ".json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        documents[name] = schema
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    source = files("app.comment_export").joinpath("schemas", "v2")
    for name in ("comment", "manifest"):
        schema = json.loads(source.joinpath(name + ".json").read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    return documents, registry


@lru_cache(maxsize=7)
def _validator(kind):
    schemas, registry = _schemas()
    return Draft202012Validator(schemas[kind], registry=registry, format_checker=FormatChecker())


def _finite(value):
    if (isinstance(value, float) and not math.isfinite(value)) or (
        isinstance(value, Decimal) and not value.is_finite()
    ):
        raise PacketError("nonfinite_json_number")
    if isinstance(value, dict):
        for item in value.values():
            _finite(item)
    elif isinstance(value, list):
        for item in value:
            _finite(item)


def validate_document(kind: str, value: dict, *, execution=False) -> None:
    """Validate required fields and conditional types, without accessing referenced files.

    The publishing validator checks identities, source projections, coverage, paths and hashes.
    Unknown same-major fields remain data; they cannot register or fetch additional schemas.
    """
    kind = kind.replace("_", "-") if isinstance(kind, str) else ""
    if kind not in KINDS:
        raise PacketError("unknown_document_kind")
    code = "invalid_" + kind.replace("-", "_")
    if not isinstance(value, dict):
        raise PacketError(code)
    _finite(value)
    if kind in {"packet", "manifest", "group-coverage", "member-registry"}:
        version = value.get("schema_version")
        if not isinstance(version, str) or re.fullmatch(r"2\.\d+\.\d+", version) is None:
            raise PacketError("unsupported_schema_version")
    try:
        invalid = next(_validator(kind).iter_errors(value), None)
    except Unresolvable:
        raise PacketError("schema_reference_unavailable") from None
    if invalid is not None:
        raise PacketError(code)
    if kind == "packet" and execution and value["response_contract"]["schema_ref"] is None:
        raise PacketError("output_contract_missing")
