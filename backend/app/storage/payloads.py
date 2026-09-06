"""Content identity independent of observation metadata and export batches."""

import hashlib
import json
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import delete, select, tuple_
from sqlalchemy.dialects.postgresql import insert

from .codec import decode_json, encode_json
from .errors import StorageError
from .mapping import COMMENT_KEYS
from .schema import comment_payloads, comments


def payload_document(record: dict) -> dict:
    return {
        "content": record["content"],
        "extra_fields": {
            "top": {key: value for key, value in record.items() if key not in COMMENT_KEYS},
            "author": {
                key: value for key, value in record["author"].items()
                if key not in {"uid", "nickname"}
            },
        },
    }


def _normalize_numbers(value):
    # JSONB expands integral exponents and drops negative zero on readback.
    if isinstance(value, float) and value.is_integer():
        return int(Decimal(str(value)))
    if isinstance(value, list):
        return [_normalize_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_numbers(item) for key, item in value.items()}
    return value


def payload_digest(document: dict) -> str:
    canonical = json.dumps(
        _normalize_numbers(document), sort_keys=True, ensure_ascii=True,
        separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def resolve_payloads(conn, video_id: str, records: list[dict]) -> dict[str, str]:
    """Resolve one bounded import chunk, inside the caller's video lock and transaction."""
    documents = {}
    for record in records:
        if record["video_id"] != video_id:
            raise StorageError("payload_identity_conflict")
        cid = record["comment_id"]
        document = payload_document(record)
        if cid in documents and documents[cid] != document:
            raise StorageError("payload_identity_conflict")
        documents[cid] = document
    if not documents:
        return {}
    hashes = {cid: payload_digest(doc) for cid, doc in documents.items()}
    p = comment_payloads.c
    keys = [(cid, hashes[cid]) for cid in documents]
    query = select(comment_payloads).where(
        p.video_id == video_id, tuple_(p.comment_id, p.content_hash).in_(keys)
    )
    existing = {row["comment_id"]: row for row in conn.execute(query).mappings()}
    missing = [
        {
            "payload_id": str(uuid4()), "video_id": video_id, "comment_id": cid,
            "content_hash": hashes[cid], "content": encode_json(doc["content"]),
            "extra_fields": encode_json(doc["extra_fields"]),
        }
        for cid, doc in documents.items() if cid not in existing
    ]
    if missing:
        conn.execute(insert(comment_payloads).on_conflict_do_nothing(
            constraint="uq_comment_payload_hash"
        ), missing)
        existing = {row["comment_id"]: row for row in conn.execute(query).mappings()}
    result = {}
    for cid, document in documents.items():
        row = existing.get(cid)
        if row is None:
            raise StorageError("payload_resolution_failed")
        stored = {"content": decode_json(row["content"]),
                  "extra_fields": decode_json(row["extra_fields"])}
        if payload_digest(stored) != hashes[cid] or stored != document:
            raise StorageError("payload_hash_conflict")
        result[cid] = str(row["payload_id"])
    return result


def prune_payloads(conn, video_id: str) -> int:
    """Caller holds video ownership; never remove a payload referenced by any state."""
    p = comment_payloads.c
    referenced = select(comments.c.payload_id).where(
        comments.c.video_id == p.video_id,
        comments.c.comment_id == p.comment_id,
        comments.c.payload_id == p.payload_id,
    ).exists()
    result = conn.execute(delete(comment_payloads).where(p.video_id == video_id, ~referenced))
    return result.rowcount
