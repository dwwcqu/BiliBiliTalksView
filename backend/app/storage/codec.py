"""Reversible pg-text-v1 mapping; never alter external protocol strings."""

import re
from typing import Any

from .errors import StorageError


def encode_text(value: str) -> str:
    result = []
    for char in value:
        if char == "\\":
            result.append("\\\\")
        elif char == "\0":
            result.append(r"\0")
        elif 0xD800 <= ord(char) <= 0xDFFF:
            result.append("\\u" + format(ord(char), "04X"))
        else:
            result.append(char)
    return "".join(result)


def decode_text(value: str) -> str:
    result = []
    i = 0
    while i < len(value):
        if value[i] != "\\":
            result.append(value[i])
            i += 1
        elif value[i : i + 2] == "\\\\":
            result.append("\\")
            i += 2
        elif value[i : i + 2] == r"\0":
            result.append("\0")
            i += 2
        elif value[i : i + 2] == r"\u" and re.fullmatch(
            r"D[89A-F][0-9A-F]{2}", value[i + 2 : i + 6]
        ):
            result.append(chr(int(value[i + 2 : i + 6], 16)))
            i += 6
        else:
            raise StorageError("storage_decode_error")
    return "".join(result)


def _map(value: Any, transform) -> Any:
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, list):
        return [_map(item, transform) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            mapped_key = transform(key)
            if mapped_key in result:
                raise StorageError("storage_decode_error")
            result[mapped_key] = _map(item, transform)
        return result
    return value


def encode_json(value: Any) -> Any:
    return _map(value, encode_text)


def decode_json(value: Any) -> Any:
    return _map(value, decode_text)
