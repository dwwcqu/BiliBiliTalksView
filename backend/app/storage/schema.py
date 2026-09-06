"""SQLAlchemy Core schema, storage codec version pg-text-v1."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()
STORAGE_CODEC = "pg-text-v1"


def _id_check(column: str) -> CheckConstraint:
    return CheckConstraint(f"{column} ~ '^[1-9][0-9]*$'", name=f"ck_{column}_positive_id")


def _count(column: str, nullable: bool = False) -> Column:
    return Column(column, BigInteger, CheckConstraint(f"{column} >= 0"), nullable=nullable)


videos = Table(
    "videos",
    metadata,
    Column("video_id", Text, primary_key=True),
    Column("cache_version", BigInteger, nullable=False, server_default=text("0")),
    Column("collection_requested", Boolean, nullable=False, server_default=text("false")),
    CheckConstraint("cache_version >= 0", name="ck_cache_version"),
    Column("platform", Text, nullable=False),
    Column("aid", Text, nullable=False),
    Column("oid", Text, nullable=False),
    Column("comment_type", Integer, nullable=False),
    Column("bvid", Text),
    Column("episode_id", Text),
    Column("current_state_id", UUID(as_uuid=False)),
    Column("working_state_id", UUID(as_uuid=False)),
    UniqueConstraint("platform", "comment_type", "oid", name="uq_video_source"),
    _id_check("aid"),
    _id_check("oid"),
    _id_check("episode_id"),
    CheckConstraint(
        "platform = 'bilibili' AND comment_type = 1 AND aid = oid", name="ck_video_source"
    ),
    CheckConstraint("video_id = 'bilibili:video:' || aid", name="ck_video_identity"),
    CheckConstraint(
        "current_state_id IS NULL OR working_state_id IS NULL OR "
        "current_state_id <> working_state_id",
        name="ck_distinct_pointers",
    ),
)
video_links = Table(
    "video_links",
    metadata,
    Column("normalized_url", Text, primary_key=True),
    Column("video_id", Text, ForeignKey("videos.video_id", ondelete="CASCADE"), nullable=False),
)
discussion_states = Table(
    "discussion_states",
    metadata,
    Column("state_id", UUID(as_uuid=False), primary_key=True),
    Column("video_id", Text, ForeignKey("videos.video_id"), nullable=False),
    Column("source_export_id", UUID(as_uuid=False), nullable=False),
    Column("schema_version", Text, nullable=False),
    Column("hour_bucket", DateTime(timezone=True), nullable=False),
    Column("captured_from", DateTime(timezone=True), nullable=False),
    Column("captured_to", DateTime(timezone=True), nullable=False),
    Column("exported_at", DateTime(timezone=True), nullable=False),
    Column("coverage", JSONB, nullable=False),
    Column("title", Text),
    Column("source_metadata", JSONB, nullable=False),
    Column("refresh_context", JSONB),
    Column("lifecycle", Text, nullable=False),
    UniqueConstraint("video_id", "state_id", name="uq_video_state"),
    CheckConstraint(
        "lifecycle IN ('loading','ready','current','partial','failed')", name="ck_state_lifecycle"
    ),
    CheckConstraint(
        "captured_from <= captured_to AND captured_to <= exported_at", name="ck_capture_times"
    ),
)
Index(
    "uq_one_working_state",
    discussion_states.c.video_id,
    unique=True,
    postgresql_where=text("lifecycle <> 'current'"),
)
for pointer in ("current_state_id", "working_state_id"):
    videos.append_constraint(
        ForeignKeyConstraint(
            ["video_id", pointer],
            ["discussion_states.video_id", "discussion_states.state_id"],
            name=f"fk_video_{pointer}",
            use_alter=True,
        )
    )

threads = Table(
    "threads",
    metadata,
    Column(
        "state_id",
        UUID(as_uuid=False),
        ForeignKey("discussion_states.state_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("root_id", Text(collation="C"), primary_key=True),
    Column("root_author_uid", Text),
    Column("root_author_name", Text),
    Column("source_title", Text),
    _count("comment_count"),
    _count("reply_count"),
    _count("participant_count"),
    _count("unknown_author_comment_count"),
    Column("coverage", JSONB, nullable=False),
    Column("source_metadata", JSONB, nullable=False),
    _id_check("root_id"),
    _id_check("root_author_uid"),
)
comment_payloads = Table(
    "comment_payloads",
    metadata,
    Column("payload_id", UUID(as_uuid=False), primary_key=True),
    Column("video_id", Text, ForeignKey("videos.video_id"), nullable=False),
    Column("comment_id", Text(collation="C"), nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("content", JSONB, nullable=False),
    Column("extra_fields", JSONB, nullable=False),
    UniqueConstraint("video_id", "comment_id", "content_hash", name="uq_comment_payload_hash"),
    UniqueConstraint("video_id", "comment_id", "payload_id", name="uq_comment_payload_identity"),
    _id_check("comment_id"),
    CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_payload_hash"),
)
comments = Table(
    "comments",
    metadata,
    Column("state_id", UUID(as_uuid=False), primary_key=True),
    Column("comment_id", Text(collation="C"), primary_key=True),
    Column("root_id", Text(collation="C"), nullable=False),
    Column("parent_id", Text),
    Column("kind", Text, nullable=False),
    Column("author_uid", Text),
    Column("nickname", Text),
    Column("created_at", DateTime(timezone=True)),
    Column("collected_at", DateTime(timezone=True), nullable=False),
    _count("like_count", nullable=True),
    Column("reply_relation", JSONB, nullable=False),
    Column("video_id", Text, nullable=False),
    Column("payload_id", UUID(as_uuid=False), nullable=False),
    ForeignKeyConstraint(
        ["video_id", "state_id"], ["discussion_states.video_id", "discussion_states.state_id"],
        ondelete="CASCADE", name="fk_comment_state",
    ),
    ForeignKeyConstraint(
        ["video_id", "comment_id", "payload_id"],
        ["comment_payloads.video_id", "comment_payloads.comment_id", "comment_payloads.payload_id"],
        name="fk_comment_payload",
    ),
    Column(
        "root_rank", Integer, Computed("CASE WHEN kind = 'root' THEN 0 ELSE 1 END", persisted=True)
    ),
    Column("id_length", Integer, Computed("length(comment_id)", persisted=True)),
    ForeignKeyConstraint(
        ["state_id", "root_id"],
        ["threads.state_id", "threads.root_id"],
        ondelete="CASCADE",
        name="fk_comment_thread",
    ),
    _id_check("comment_id"),
    _id_check("root_id"),
    _id_check("parent_id"),
    _id_check("author_uid"),
    CheckConstraint("kind IN ('root','reply')", name="ck_comment_kind"),
    CheckConstraint(
        "(kind = 'root' AND root_id = comment_id AND parent_id IS NULL) OR "
        "(kind = 'reply' AND root_id <> comment_id)",
        name="ck_comment_root",
    ),
    CheckConstraint("parent_id IS NULL OR parent_id <> comment_id", name="ck_parent_not_self"),
)
Index(
    "ix_comments_thread_order",
    comments.c.state_id,
    comments.c.root_id,
    comments.c.root_rank,
    comments.c.created_at.asc().nulls_last(),
    comments.c.id_length,
    comments.c.comment_id,
)
Index(
    "ix_comments_user_order",
    comments.c.state_id,
    comments.c.author_uid,
    comments.c.created_at.asc().nulls_last(),
    comments.c.id_length,
    comments.c.comment_id,
)
unclassified_comments = Table(
    "unclassified_comments",
    metadata,
    Column(
        "state_id",
        UUID(as_uuid=False),
        ForeignKey("discussion_states.state_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("ordinal", BigInteger, primary_key=True),
    Column("payload", JSONB, nullable=False),
    CheckConstraint("ordinal >= 0", name="ck_unclassified_ordinal"),
)
import_receipts = Table(
    "import_receipts",
    metadata,
    Column("video_id", Text, ForeignKey("videos.video_id"), primary_key=True),
    Column("source_export_id", UUID(as_uuid=False), primary_key=True),
    Column("canonical_digest", Text, nullable=False),
    Column("state_id", UUID(as_uuid=False)),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
    Column("safe_error_code", Text),
    ForeignKeyConstraint(
        ["video_id", "state_id"],
        ["discussion_states.video_id", "discussion_states.state_id"],
        name="fk_receipt_state",
    ),
    CheckConstraint(
        "status IN ('loading','ready','partial','published','failed','expired')",
        name="ck_receipt_status",
    ),
    CheckConstraint("canonical_digest ~ '^[0-9a-f]{64}$'", name="ck_receipt_digest"),
)

Index("ix_comments_payload", comments.c.video_id, comments.c.comment_id, comments.c.payload_id)
