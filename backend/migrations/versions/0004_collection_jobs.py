"""Add persistent collection and URL-resolution queues."""

from alembic import op

revision = "0004_collection_jobs"
down_revision = "0003_cache_handoff"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE videos ADD COLUMN collection_requested BOOLEAN NOT NULL DEFAULT false")
    op.execute("""
    CREATE TABLE collection_jobs (
      job_id UUID PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(video_id), input_url TEXT NOT NULL,
      requested_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), hour_bucket TIMESTAMPTZ NOT NULL,
      status TEXT NOT NULL CONSTRAINT ck_collection_job_status CHECK (status IN ('queued','running','waiting_source','blocked','succeeded','partial','failed','cancelled')),
      phase TEXT NOT NULL DEFAULT 'queued', requested_mode TEXT NOT NULL DEFAULT 'auto' CONSTRAINT ck_collection_job_requested_mode CHECK (requested_mode IN ('auto','full')),
      effective_mode TEXT CONSTRAINT ck_collection_job_effective_mode CHECK (effective_mode IS NULL OR effective_mode IN ('full','incremental')),
      baseline_version BIGINT CONSTRAINT ck_collection_job_baseline CHECK (baseline_version IS NULL OR baseline_version >= 0), base_state_id UUID,
      owner_token UUID, lease_until TIMESTAMPTZ, heartbeat_at TIMESTAMPTZ, cancel_requested BOOLEAN NOT NULL DEFAULT false,
      action TEXT CONSTRAINT ck_collection_job_action CHECK (action IS NULL OR action IN ('retry','recover')),
      attempt_count INTEGER NOT NULL DEFAULT 0, retry_count INTEGER NOT NULL DEFAULT 0, not_before TIMESTAMPTZ,
      progress JSONB NOT NULL DEFAULT '{}'::jsonb, requests INTEGER NOT NULL DEFAULT 0, max_requests INTEGER NOT NULL DEFAULT 12000,
      result_state_id UUID REFERENCES discussion_states(state_id) ON DELETE SET NULL, completed_at TIMESTAMPTZ, safe_error TEXT,
      CONSTRAINT ck_collection_job_counts CHECK (attempt_count >= 0 AND retry_count >= 0 AND requests >= 0 AND max_requests >= 0)
    );
    CREATE UNIQUE INDEX uq_collection_job_hour ON collection_jobs(video_id,hour_bucket);
    CREATE UNIQUE INDEX uq_collection_job_active ON collection_jobs(video_id) WHERE status IN ('queued','running','waiting_source','blocked');
    CREATE TABLE resolution_requests (
      request_id UUID PRIMARY KEY, normalized_url TEXT NOT NULL, accepted_at TIMESTAMPTZ NOT NULL, hour_bucket TIMESTAMPTZ NOT NULL,
      status TEXT NOT NULL CONSTRAINT ck_resolution_request_status CHECK (status IN ('queued','resolving','ready','waiting_source','blocked','failed')),
      video_id TEXT REFERENCES videos(video_id), job_id UUID REFERENCES collection_jobs(job_id) ON DELETE SET NULL,
      requests INTEGER NOT NULL DEFAULT 0, max_requests INTEGER NOT NULL DEFAULT 6, retry_count INTEGER NOT NULL DEFAULT 0,
      owner_token UUID, lease_until TIMESTAMPTZ, heartbeat_at TIMESTAMPTZ, not_before TIMESTAMPTZ, completed_at TIMESTAMPTZ,
      action TEXT CONSTRAINT ck_resolution_request_action CHECK (action IS NULL OR action IN ('retry','recover')), safe_error TEXT,
      CONSTRAINT ck_resolution_request_counts CHECK (requests >= 0 AND max_requests >= 0 AND max_requests <= 6 AND retry_count >= 0)
    );
    CREATE UNIQUE INDEX uq_resolution_request_hour ON resolution_requests(normalized_url,hour_bucket);
    CREATE TABLE source_runtime (
      id INTEGER PRIMARY KEY CONSTRAINT ck_source_runtime_singleton CHECK (id=1),
      source_gate TEXT NOT NULL DEFAULT 'normal' CONSTRAINT ck_source_runtime_gate CHECK (source_gate IN ('normal','needs_operator','recovering')),
      worker_token UUID, owner_backend_pid INTEGER,
      blocked_kind TEXT CONSTRAINT ck_source_runtime_blocked_kind CHECK (blocked_kind IS NULL OR blocked_kind IN ('job','resolution')),
      blocked_id UUID, safe_error TEXT, blocked_at TIMESTAMPTZ,
      action TEXT CONSTRAINT ck_source_runtime_action CHECK (action IS NULL OR action='revalidate'), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    INSERT INTO source_runtime(id) VALUES (1);
    """)


def downgrade():
    op.execute("DROP TABLE source_runtime")
    op.execute("DROP TABLE resolution_requests")
    op.execute("DROP TABLE collection_jobs")
    op.execute("ALTER TABLE videos DROP COLUMN collection_requested")
