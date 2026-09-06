"""Strict JSON parsing and the versioned on-disk export contract."""
import json
import re
from datetime import datetime
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


class ContractError(ValueError):
    """A safe error code describing invalid exported data."""


def parse_json(text: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError("duplicate_json_key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ContractError("nonfinite_json_number")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, TypeError) as exc:
        raise ContractError("invalid_json") from exc


@lru_cache(maxsize=14)
def _validator(kind: str, major: str) -> Draft202012Validator:
    if kind not in {"comment", "manifest", "thread", "user", "current", "unclassified"}:
        raise ContractError("unknown_record_kind")
    folder = files("app.comment_export").joinpath("schemas")
    path = folder.joinpath("v2", kind + ".json") if major == "2" else folder.joinpath(kind + ".json")
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    checker = FormatChecker()

    @checker.checks("date-time", raises=ValueError)
    def valid_datetime(value):
        if not isinstance(value, str):
            return True
        return datetime.fromisoformat(value).tzinfo is not None

    return Draft202012Validator(schema, format_checker=checker)


def validate_record(kind: str, value: dict) -> None:
    if not isinstance(value, dict):
        raise ContractError("invalid_" + kind)
    version = "1.0.0" if kind == "unclassified" else value.get("schema_version", "")
    match = re.fullmatch(r"([12])\.\d+\.\d+", version) if isinstance(version, str) else None
    if not match:
        raise ContractError("unsupported_schema_version")
    if next(_validator(kind, match[1]).iter_errors(value), None) is not None:
        raise ContractError("invalid_" + kind)
    if kind == "comment":
        root = value["kind"] == "root"
        parent = value["parent_id"]
        relation = value["reply_relation"]
        expected = "not_applicable" if root else "unknown" if parent is None else "source"
        if relation["status"] != expected or parent == value["comment_id"]:
            raise ContractError("invalid_reply_relation")
        if root and (value["root_id"] != value["comment_id"] or parent is not None
                     or relation["target_uid"] is not None):
            raise ContractError("invalid_root")
        if not root and value["root_id"] == value["comment_id"]:
            raise ContractError("invalid_reply")
    elif kind == "user":
        if (value["uid"] is None) != (value["identity_status"] == "unknown"):
            raise ContractError("invalid_user_identity")
    elif kind == "current":
        if value["batch_path"] != "batches/" + value["export_id"]:
            raise ContractError("invalid_batch_path")
    elif kind == "manifest":
        if not (value["captured_from"] <= value["captured_to"] <= value["exported_at"]):
            raise ContractError("invalid_capture_interval")
        source = value["source"]
        if source["aid"] != source["oid"] or value["video_id"] != "bilibili:video:" + source["aid"]:
            raise ContractError("invalid_video_identity")
        coverage = value["coverage"]
        if coverage["status"] == "verified" and (
            coverage["main_pagination"] != "verified"
            or coverage["replies_pagination"] != "verified"
            or value["counts"]["unclassified_records"] > 0
        ):
            raise ContractError("invalid_coverage")
