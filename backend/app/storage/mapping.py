"""One encoding boundary between protocol objects and relational columns."""

from datetime import UTC, datetime

from .codec import decode_json, decode_text, encode_json, encode_text
from .errors import StorageError

COMMENT_KEYS = {
    "schema_version",
    "export_id",
    "video_id",
    "comment_id",
    "root_id",
    "parent_id",
    "kind",
    "author",
    "created_at",
    "collected_at",
    "like_count",
    "reply_relation",
    "content",
}


def date_value(value: str | None):
    return datetime.fromisoformat(value) if value else None


def date_text(value):
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def encoded_optional(value):
    return encode_text(value) if value is not None else None


def decoded_optional(value):
    return decode_text(value) if value is not None else None


def comment_values(record: dict, state_id: str) -> dict:
    return {
        "state_id": state_id,
        "comment_id": record["comment_id"],
        "root_id": record["root_id"],
        "parent_id": record["parent_id"],
        "kind": record["kind"],
        "author_uid": record["author"]["uid"],
        "nickname": encoded_optional(record["author"]["nickname"]),
        "created_at": date_value(record["created_at"]),
        "collected_at": date_value(record["collected_at"]),
        "like_count": record["like_count"],
        "reply_relation": encode_json(record["reply_relation"]),
        "content": encode_json(record["content"]),
        "extra_fields": encode_json(
            {
                "top": {k: v for k, v in record.items() if k not in COMMENT_KEYS},
                "author": {
                    k: v for k, v in record["author"].items() if k not in {"uid", "nickname"}
                },
            }
        ),
    }


def record_from_row(row, state) -> dict:
    extra = decode_json(row["extra_fields"])
    if set(extra.get("top", {})) & COMMENT_KEYS or set(extra.get("author", {})) & {
        "uid",
        "nickname",
    }:
        raise StorageError("storage_mapping_conflict")
    return {
        **extra.get("top", {}),
        "schema_version": state["schema_version"],
        "export_id": str(state["source_export_id"]),
        "video_id": state["video_id"],
        "comment_id": row["comment_id"],
        "root_id": row["root_id"],
        "parent_id": row["parent_id"],
        "kind": row["kind"],
        "author": {
            **extra.get("author", {}),
            "uid": row["author_uid"],
            "nickname": decoded_optional(row["nickname"]),
        },
        "created_at": date_text(row["created_at"]),
        "collected_at": date_text(row["collected_at"]),
        "like_count": row["like_count"],
        "reply_relation": decode_json(row["reply_relation"]),
        "content": decode_json(row["content"]),
    }


def member_values(record: dict, state_id: str, payload_id: str) -> dict:
    values = comment_values(record, state_id)
    values.pop("content")
    values.pop("extra_fields")
    return {**values, "video_id": record["video_id"], "payload_id": payload_id}
