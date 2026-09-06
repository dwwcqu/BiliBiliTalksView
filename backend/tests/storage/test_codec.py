import pytest

from app.storage.codec import decode_json, decode_text, encode_json, encode_text
from app.storage.errors import StorageError


@pytest.mark.parametrize("value", ["普通正文", "a\0b", r"a\0b", "\\", "\ud800", "\u2028"])
def test_text_roundtrip(value):
    stored = encode_text(value)
    assert "\0" not in stored
    assert decode_text(stored) == value


def test_nested_keys_and_literals_do_not_collide():
    raw = {"\0": [r"\0", "\0", {"\ud800": "\udfff"}]}
    assert decode_json(encode_json(raw)) == raw
    assert encode_text("\0") != encode_text(r"\0")


@pytest.mark.parametrize("value", ["\\", r"\x", r"\u0041", r"\uD80"])
def test_invalid_storage_escape_rejected(value):
    with pytest.raises(StorageError):
        decode_text(value)
