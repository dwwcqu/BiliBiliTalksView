"""Add cache handoff versioning and persisted refresh context."""

from alembic import op

revision = "0003_cache_handoff"
down_revision = "0002_comment_payloads"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE videos ADD COLUMN cache_version BIGINT NOT NULL DEFAULT 0, "
        "ADD CONSTRAINT ck_cache_version CHECK (cache_version >= 0)"
    )
    op.execute("ALTER TABLE discussion_states ADD COLUMN refresh_context JSONB")
    op.execute(
        """
        CREATE FUNCTION fn_videos_guard_cache_version() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.cache_version < OLD.cache_version THEN
                RAISE EXCEPTION USING MESSAGE = 'cache_version_regression';
            END IF;
            IF NEW.current_state_id IS DISTINCT FROM OLD.current_state_id
               OR NEW.working_state_id IS DISTINCT FROM OLD.working_state_id THEN
                NEW.cache_version := GREATEST(NEW.cache_version, OLD.cache_version) + 1;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_videos_guard_cache_version "
        "BEFORE UPDATE ON videos FOR EACH ROW "
        "EXECUTE FUNCTION fn_videos_guard_cache_version()"
    )
    op.execute(
        """
        CREATE FUNCTION fn_discussion_states_bump_cache_version() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.lifecycle IS DISTINCT FROM OLD.lifecycle
               OR NEW.refresh_context IS DISTINCT FROM OLD.refresh_context THEN
                UPDATE videos
                SET cache_version = cache_version + 1
                WHERE video_id = NEW.video_id
                  AND (current_state_id = NEW.state_id OR working_state_id = NEW.state_id);
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_discussion_states_bump_cache_version "
        "AFTER UPDATE OF lifecycle, refresh_context ON discussion_states FOR EACH ROW "
        "EXECUTE FUNCTION fn_discussion_states_bump_cache_version()"
    )


def downgrade():
    op.execute(
        "DROP TRIGGER IF EXISTS trg_discussion_states_bump_cache_version "
        "ON discussion_states"
    )
    op.execute("DROP FUNCTION IF EXISTS fn_discussion_states_bump_cache_version()")
    op.execute("DROP TRIGGER IF EXISTS trg_videos_guard_cache_version ON videos")
    op.execute("DROP FUNCTION IF EXISTS fn_videos_guard_cache_version()")
    op.execute("ALTER TABLE discussion_states DROP COLUMN refresh_context")
    op.execute("ALTER TABLE videos DROP CONSTRAINT ck_cache_version")
    op.execute("ALTER TABLE videos DROP COLUMN cache_version")