"""Persistent queue tables sharing the discussion storage metadata."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.storage.schema import metadata

collection_jobs = Table(
    "collection_jobs",
    metadata,
    Column("job_id", UUID(as_uuid=False), primary_key=True),
    Column("video_id", Text, ForeignKey("videos.video_id"), nullable=False),
    Column("input_url", Text, nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("hour_bucket", DateTime(timezone=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("phase", Text, nullable=False, server_default=text("'queued'")),
    Column("requested_mode", Text, nullable=False, server_default=text("'auto'")),
    Column("effective_mode", Text),
    Column("baseline_version", BigInteger),
    Column("base_state_id", UUID(as_uuid=False)),
    Column("owner_token", UUID(as_uuid=False)),
    Column("lease_until", DateTime(timezone=True)),
    Column("heartbeat_at", DateTime(timezone=True)),
    Column("cancel_requested", Boolean, nullable=False, server_default=text("false")),
    Column("action", Text),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("retry_count", Integer, nullable=False, server_default=text("0")),
    Column("not_before", DateTime(timezone=True)),
    Column("progress", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("requests", Integer, nullable=False, server_default=text("0")),
    Column("max_requests", Integer, nullable=False, server_default=text("12000")),
    Column(
        "result_state_id",
        UUID(as_uuid=False),
        ForeignKey("discussion_states.state_id", ondelete="SET NULL"),
    ),
    Column("completed_at", DateTime(timezone=True)),
    Column("safe_error", Text),
    CheckConstraint(
        "status IN ('queued','running','waiting_source','blocked','succeeded','partial','failed','cancelled')",
        name="ck_collection_job_status",
    ),
    CheckConstraint("requested_mode IN ('auto','full')", name="ck_collection_job_requested_mode"),
    CheckConstraint(
        "effective_mode IS NULL OR effective_mode IN ('full','incremental')",
        name="ck_collection_job_effective_mode",
    ),
    CheckConstraint(
        "baseline_version IS NULL OR baseline_version >= 0", name="ck_collection_job_baseline"
    ),
    CheckConstraint(
        "action IS NULL OR action IN ('retry','recover')", name="ck_collection_job_action"
    ),
    CheckConstraint(
        "attempt_count >= 0 AND retry_count >= 0 AND requests >= 0 AND max_requests >= 0",
        name="ck_collection_job_counts",
    ),
)
Index(
    "uq_collection_job_hour", collection_jobs.c.video_id, collection_jobs.c.hour_bucket, unique=True
)
Index(
    "uq_collection_job_active",
    collection_jobs.c.video_id,
    unique=True,
    postgresql_where=collection_jobs.c.status.in_(
        ["queued", "running", "waiting_source", "blocked"]
    ),
)

resolution_requests = Table(
    "resolution_requests",
    metadata,
    Column("request_id", UUID(as_uuid=False), primary_key=True),
    Column("normalized_url", Text, nullable=False),
    Column("accepted_at", DateTime(timezone=True), nullable=False),
    Column("hour_bucket", DateTime(timezone=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("video_id", Text, ForeignKey("videos.video_id")),
    Column(
        "job_id", UUID(as_uuid=False), ForeignKey("collection_jobs.job_id", ondelete="SET NULL")
    ),
    Column("requests", Integer, nullable=False, server_default=text("0")),
    Column("max_requests", Integer, nullable=False, server_default=text("6")),
    Column("retry_count", Integer, nullable=False, server_default=text("0")),
    Column("owner_token", UUID(as_uuid=False)),
    Column("lease_until", DateTime(timezone=True)),
    Column("heartbeat_at", DateTime(timezone=True)),
    Column("not_before", DateTime(timezone=True)),
    Column("completed_at", DateTime(timezone=True)),
    Column("action", Text),
    Column("safe_error", Text),
    CheckConstraint(
        "status IN ('queued','resolving','ready','waiting_source','blocked','failed')",
        name="ck_resolution_request_status",
    ),
    CheckConstraint(
        "requests >= 0 AND max_requests >= 0 AND max_requests <= 6 AND retry_count >= 0",
        name="ck_resolution_request_counts",
    ),
    CheckConstraint(
        "action IS NULL OR action IN ('retry','recover')", name="ck_resolution_request_action"
    ),
)
Index(
    "uq_resolution_request_hour",
    resolution_requests.c.normalized_url,
    resolution_requests.c.hour_bucket,
    unique=True,
)

source_runtime = Table(
    "source_runtime",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("source_gate", Text, nullable=False, server_default=text("'normal'")),
    Column("worker_token", UUID(as_uuid=False)),
    Column("owner_backend_pid", Integer),
    Column("blocked_kind", Text),
    Column("blocked_id", UUID(as_uuid=False)),
    Column("safe_error", Text),
    Column("blocked_at", DateTime(timezone=True)),
    Column("action", Text),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("id = 1", name="ck_source_runtime_singleton"),
    CheckConstraint(
        "source_gate IN ('normal','needs_operator','recovering')", name="ck_source_runtime_gate"
    ),
    CheckConstraint(
        "blocked_kind IS NULL OR blocked_kind IN ('job','resolution')",
        name="ck_source_runtime_blocked_kind",
    ),
    CheckConstraint("action IS NULL OR action = 'revalidate'", name="ck_source_runtime_action"),
)
