"""Local-only AnalysisOutput 1.0 structural validation."""

import json
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Resource
from referencing.exceptions import Unresolvable

from app.analysis_packets.contract import _finite
from app.analysis_packets.contract import _schemas as input_schemas
from app.analysis_packets.errors import PacketError

from .errors import OutputError

DIMENSIONS = (
    "topic_stance",
    "discourse_function",
    "argument_support",
    "response_engagement",
    "expressed_emotion",
    "interpersonal_expression",
    "conflict_cooperation",
    "view_revision",
)
KINDS = frozenset({"task-result", "validation-receipt", "user-profile"})
FORMATS = FormatChecker()


@FORMATS.checks("date-time", raises=(ValueError, TypeError))
def _date_time(value):
    if not isinstance(value, str):
        return True
    datetime.fromisoformat(value)
    return True


def _kind(kind):
    kind = kind.replace("_", "-") if isinstance(kind, str) else ""
    if kind == "profile-candidate":
        kind = "user-profile"
    if kind not in KINDS:
        raise OutputError("unknown_document_kind")
    return kind


@lru_cache(maxsize=1)
def _schemas():
    _, registry = input_schemas()
    documents = {}
    folder = files("app.analysis_results").joinpath("schemas")
    for name in sorted(KINDS | {"common"}):
        document = json.loads(folder.joinpath(name + ".json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(document)
        documents[name] = document
        registry = registry.with_resource(document["$id"], Resource.from_contents(document))
    return documents, registry


def schema_document(kind: str) -> dict:
    """Return a detached schema; every reference resolves through our local registry."""
    return deepcopy(_schemas()[0][_kind(kind)])


@lru_cache(maxsize=3)
def _validator(kind):
    documents, registry = _schemas()
    return Draft202012Validator(documents[kind], registry=registry, format_checker=FORMATS)


def validate_document(kind: str, value: dict) -> None:
    kind = _kind(kind)
    try:
        _finite(value)
        if next(_validator(kind).iter_errors(value), None) is not None:
            raise OutputError("invalid_" + kind.replace("-", "_"))
    except OutputError:
        raise
    except PacketError as error:
        raise OutputError(error.code) from None
    except Unresolvable:
        raise OutputError("schema_reference_unavailable") from None
    except (TypeError, ValueError, RecursionError):
        raise OutputError("invalid_" + kind.replace("-", "_")) from None
