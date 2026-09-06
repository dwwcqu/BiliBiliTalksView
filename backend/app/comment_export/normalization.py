"""Normalize source comments without inferring identities or reply targets."""
import re
from datetime import UTC, datetime

from .contract import ContractError, validate_record


def external_id(value) -> str | None:
    if type(value) in (str, int) and re.fullmatch(r"[1-9][0-9]*", str(value)):
        return str(value)
    return None


def timestamp(value) -> str | None:
    if type(value) not in (int, float) or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError, OSError):
        return None


def nonnegative(value):
    return value if type(value) is int and value >= 0 else None


def text(value):
    return value if isinstance(value, str) else None


def normalize_comment(raw: dict, root: str | None, video_id: str, export_id: str,
                      observed_at: str) -> dict:
    cid = external_id(raw.get("rpid_str", raw.get("rpid")))
    if cid is None:
        raise ContractError("invalid_comment_id")
    observed_root = external_id(raw.get("root_str", raw.get("root")))
    if root is not None and observed_root is not None and root != observed_root:
        raise ContractError("invalid_root_id")
    root_id = root or observed_root or cid
    is_root = root_id == cid
    parent = None if is_root else external_id(raw.get("parent_str", raw.get("parent")))
    member = raw.get("member") if isinstance(raw.get("member"), dict) else {}
    content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
    images = []
    for item in content.get("pictures") or []:
        if isinstance(item, dict):
            images.append({"url": text(item.get("img_src")),
                           "width": nonnegative(item.get("img_width")) or None,
                           "height": nonnegative(item.get("img_height")) or None})
    emotes = []
    if isinstance(content.get("emote"), dict):
        for token, item in content["emote"].items():
            if isinstance(item, dict):
                emotes.append({"token": token, "url": text(item.get("url"))})
    row = {"schema_version": "1.0.0", "export_id": export_id, "video_id": video_id,
           "comment_id": cid, "root_id": root_id, "parent_id": parent,
           "kind": "root" if is_root else "reply",
           "author": {"uid": external_id(member.get("mid_str", member.get("mid"))),
                      "nickname": text(member.get("uname"))},
           "reply_relation": {"status": "not_applicable" if is_root else
                              "source" if parent else "unknown", "target_uid": None},
           "content": {"text": text(content.get("message")), "images": images, "emotes": emotes},
           "created_at": timestamp(raw.get("ctime")), "collected_at": observed_at,
           "like_count": nonnegative(raw.get("like"))}
    validate_record("comment", row)
    return row
