"""Add persistent API rate-limit buckets."""

from alembic import op

revision = "0005_api_rate_limits"
down_revision = "0004_collection_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE api_rate_limits (
      client_key TEXT NOT NULL CONSTRAINT ck_api_rate_limit_client_key CHECK (client_key ~ '^[0-9a-f]{64}$'),
      kind TEXT NOT NULL CONSTRAINT ck_api_rate_limit_kind CHECK (kind IN ('write','intent')),
      window_start TIMESTAMPTZ NOT NULL,
      hits INTEGER NOT NULL CONSTRAINT ck_api_rate_limit_hits CHECK (hits >= 0),
      PRIMARY KEY (client_key,kind,window_start)
    );
    CREATE INDEX ix_api_rate_limits_window_start ON api_rate_limits(window_start);
    """)


def downgrade():
    op.execute("DROP TABLE api_rate_limits")
