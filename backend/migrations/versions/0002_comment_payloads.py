"""Separate reusable comment payloads from state membership."""

import hashlib
import json
import re
from decimal import Decimal
from typing import Any
from uuid import uuid4

from alembic import op
from sqlalchemy import text

revision = "0002_comment_payloads"
down_revision = "0001_discussion_storage"
branch_labels = None
depends_on = None

_BATCH_SIZE = 500


def _map_text(value: Any, transform) -> Any:
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, list):
        return [_map_text(item, transform) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            mapped_key = transform(key)
            if mapped_key in result:
                raise RuntimeError("payload_migration_decode_error")
            result[mapped_key] = _map_text(item, transform)
        return result
    return value


def _decode_text(value: str) -> str:
    result = []
    index = 0
    while index < len(value):
        if value[index] != "\\":
            result.append(value[index])
            index += 1
        elif value[index : index + 2] == "\\\\":
            result.append("\\")
            index += 2
        elif value[index : index + 2] == r"\0":
            result.append("\0")
            index += 2
        elif value[index : index + 2] == r"\u" and re.fullmatch(
            r"D[89A-F][0-9A-F]{2}", value[index + 2 : index + 6]
        ):
            result.append(chr(int(value[index + 2 : index + 6], 16)))
            index += 6
        else:
            raise RuntimeError("payload_migration_decode_error")
    return "".join(result)


def _encode_text(value: str) -> str:
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


def _decode_json(value: Any) -> Any:
    return _map_text(value, _decode_text)


def _encode_json(value: Any) -> Any:
    return _map_text(value, _encode_text)


def _normalize_numbers(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(Decimal(str(value)))
    if isinstance(value, list):
        return [_normalize_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_numbers(item) for key, item in value.items()}
    return value


def _digest(document: dict[str, Any]) -> str:
    canonical = json.dumps(
        _normalize_numbers(document),
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _json_parameter(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _backfill(connection) -> None:
    last_state_id = None
    last_comment_id = None
    while True:
        predicate = ""
        parameters = {"batch_size": _BATCH_SIZE}
        if last_state_id is not None:
            predicate = "WHERE (c.state_id, c.comment_id) > (:last_state_id, :last_comment_id) "
            parameters.update({"last_state_id": last_state_id, "last_comment_id": last_comment_id})
        batch = (
            connection.execute(
                text(
                    "SELECT c.state_id, c.comment_id, s.video_id, c.content, c.extra_fields "
                    "FROM comments c JOIN discussion_states s ON s.state_id=c.state_id "
                    + predicate
                    + "ORDER BY c.state_id, c.comment_id LIMIT :batch_size"
                ),
                parameters,
            )
            .mappings()
            .all()
        )
        if not batch:
            break
        for row in batch:
            document = {
                "content": _decode_json(row["content"]),
                "extra_fields": _decode_json(row["extra_fields"]),
            }
            content_hash = _digest(document)
            encoded_content = _encode_json(document["content"])
            encoded_extra_fields = _encode_json(document["extra_fields"])
            payload_id = uuid4()
            inserted = connection.execute(
                text(
                    "INSERT INTO comment_payloads "
                    "(payload_id, video_id, comment_id, content_hash, content, extra_fields) "
                    "VALUES (:payload_id, :video_id, :comment_id, :content_hash, "
                    "CAST(:content AS jsonb), CAST(:extra_fields AS jsonb)) "
                    "ON CONFLICT ON CONSTRAINT uq_comment_payload_hash DO NOTHING "
                    "RETURNING payload_id"
                ),
                {
                    "payload_id": payload_id,
                    "video_id": row["video_id"],
                    "comment_id": row["comment_id"],
                    "content_hash": content_hash,
                    "content": _json_parameter(encoded_content),
                    "extra_fields": _json_parameter(encoded_extra_fields),
                },
            ).scalar_one_or_none()
            if inserted is None:
                existing = (
                    connection.execute(
                        text(
                            "SELECT payload_id, content, extra_fields FROM comment_payloads "
                            "WHERE video_id=:video_id AND comment_id=:comment_id "
                            "AND content_hash=:content_hash"
                        ),
                        {
                            "video_id": row["video_id"],
                            "comment_id": row["comment_id"],
                            "content_hash": content_hash,
                        },
                    )
                    .mappings()
                    .one()
                )
                if (
                    existing["content"] != encoded_content
                    or existing["extra_fields"] != encoded_extra_fields
                ):
                    raise RuntimeError("payload_migration_hash_conflict")
                payload_id = existing["payload_id"]
            else:
                payload_id = inserted
            connection.execute(
                text(
                    "UPDATE comments SET video_id=:video_id, payload_id=:payload_id "
                    "WHERE state_id=:state_id AND comment_id=:comment_id"
                ),
                {
                    "video_id": row["video_id"],
                    "payload_id": payload_id,
                    "state_id": row["state_id"],
                    "comment_id": row["comment_id"],
                },
            )
        last_state_id = batch[-1]["state_id"]
        last_comment_id = batch[-1]["comment_id"]


def upgrade():
    connection = op.get_bind()
    op.execute(
        "CREATE TABLE comment_payloads ("
        'payload_id UUID NOT NULL, video_id TEXT NOT NULL, comment_id TEXT COLLATE "C" NOT NULL, '
        "content_hash TEXT NOT NULL, content JSONB NOT NULL, extra_fields JSONB NOT NULL, "
        "PRIMARY KEY (payload_id), "
        "CONSTRAINT uq_comment_payload_hash UNIQUE (video_id, comment_id, content_hash), "
        "CONSTRAINT uq_comment_payload_identity UNIQUE (video_id, comment_id, payload_id), "
        "CONSTRAINT ck_comment_id_positive_id CHECK (comment_id ~ '^[1-9][0-9]*$'), "
        "CONSTRAINT ck_payload_hash CHECK (content_hash ~ '^[0-9a-f]{64}$'), "
        "FOREIGN KEY(video_id) REFERENCES videos (video_id))"
    )
    op.execute("ALTER TABLE comments ADD COLUMN video_id TEXT")
    op.execute("ALTER TABLE comments ADD COLUMN payload_id UUID")
    _backfill(connection)
    invalid = connection.execute(
        text(
            "SELECT count(*) FROM comments c LEFT JOIN comment_payloads p "
            "ON p.video_id=c.video_id AND p.comment_id=c.comment_id AND p.payload_id=c.payload_id "
            "JOIN discussion_states s ON s.state_id=c.state_id "
            "WHERE c.video_id IS NULL OR c.payload_id IS NULL OR c.video_id<>s.video_id "
            "OR p.payload_id IS NULL OR c.content<>p.content OR c.extra_fields<>p.extra_fields"
        )
    ).scalar_one()
    if invalid:
        raise RuntimeError("payload_migration_verification_failed")
    op.execute("ALTER TABLE comments ALTER COLUMN video_id SET NOT NULL")
    op.execute("ALTER TABLE comments ALTER COLUMN payload_id SET NOT NULL")
    op.execute(
        "ALTER TABLE comments ADD CONSTRAINT fk_comment_state "
        "FOREIGN KEY(video_id, state_id) REFERENCES discussion_states (video_id, state_id) "
        "ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE comments ADD CONSTRAINT fk_comment_payload "
        "FOREIGN KEY(video_id, comment_id, payload_id) "
        "REFERENCES comment_payloads (video_id, comment_id, payload_id)"
    )
    op.execute("CREATE INDEX ix_comments_payload ON comments (video_id, comment_id, payload_id)")
    op.execute("ALTER TABLE comments DROP COLUMN content")
    op.execute("ALTER TABLE comments DROP COLUMN extra_fields")


def downgrade():
    connection = op.get_bind()
    op.execute("ALTER TABLE comments ADD COLUMN content JSONB")
    op.execute("ALTER TABLE comments ADD COLUMN extra_fields JSONB")
    op.execute(
        "UPDATE comments c SET content=p.content, extra_fields=p.extra_fields "
        "FROM comment_payloads p WHERE p.video_id=c.video_id AND p.comment_id=c.comment_id "
        "AND p.payload_id=c.payload_id"
    )
    invalid = connection.execute(
        text(
            "SELECT count(*) FROM comments c LEFT JOIN comment_payloads p "
            "ON p.video_id=c.video_id AND p.comment_id=c.comment_id AND p.payload_id=c.payload_id "
            "WHERE p.payload_id IS NULL OR c.content IS NULL OR c.extra_fields IS NULL "
            "OR c.content<>p.content OR c.extra_fields<>p.extra_fields"
        )
    ).scalar_one()
    if invalid:
        raise RuntimeError("payload_downgrade_verification_failed")
    op.execute("ALTER TABLE comments ALTER COLUMN content SET NOT NULL")
    op.execute("ALTER TABLE comments ALTER COLUMN extra_fields SET NOT NULL")
    op.drop_constraint("fk_comment_payload", "comments", type_="foreignkey")
    op.drop_constraint("fk_comment_state", "comments", type_="foreignkey")
    op.drop_index("ix_comments_payload", table_name="comments")
    op.drop_column("comments", "payload_id")
    op.drop_column("comments", "video_id")
    op.drop_table("comment_payloads")
