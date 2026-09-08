"""Strict exact JSON for model-facing projections and immutable file hashes."""

import json
from decimal import Decimal


def loads(text: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("nonfinite_json_number")

    def number(raw):
        value = Decimal(raw)
        if value.is_finite() and value.adjusted() < 1000 and value == value.to_integral_value():
            return int(value)
        return value

    return json.loads(text, object_pairs_hook=pairs, parse_float=number, parse_constant=invalid)


def _encode(value):
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite_json_number")
        return str(value)
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("json_object_keys_must_be_strings")
        return (
            "{"
            + ",".join(
                json.dumps(key, ensure_ascii=False) + ":" + _encode(value[key])
                for key in sorted(value)
            )
            + "}"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def json_bytes(value) -> bytes:
    return (_encode(value) + "\n").encode("utf-8")
