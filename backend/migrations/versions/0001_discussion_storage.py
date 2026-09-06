"""Initial discussion storage; all text uses pg-text-v1."""

import re

from alembic import op
from sqlalchemy import text

revision = "0001_discussion_storage"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE TABLE videos (\n\tvideo_id TEXT NOT NULL, \n\tplatform TEXT NOT NULL, \n\taid TEXT NOT NULL, \n\toid TEXT NOT NULL, \n\tcomment_type INTEGER NOT NULL, \n\tbvid TEXT, \n\tepisode_id TEXT, \n\tcurrent_state_id UUID, \n\tworking_state_id UUID, \n\tPRIMARY KEY (video_id), \n\tCONSTRAINT uq_video_source UNIQUE (platform, comment_type, oid), \n\tCONSTRAINT ck_aid_positive_id CHECK (aid ~ '^[1-9][0-9]*$'), \n\tCONSTRAINT ck_oid_positive_id CHECK (oid ~ '^[1-9][0-9]*$'), \n\tCONSTRAINT ck_episode_id_positive_id CHECK (episode_id ~ '^[1-9][0-9]*$'), \n\tCONSTRAINT ck_video_source CHECK (platform = 'bilibili' AND comment_type = 1 AND aid = oid), \n\tCONSTRAINT ck_video_identity CHECK (video_id = 'bilibili:video:' || aid), \n\tCONSTRAINT ck_distinct_pointers CHECK (current_state_id IS NULL OR working_state_id IS NULL OR current_state_id <> working_state_id)\n)"
    )
    op.execute(
        "CREATE TABLE video_links (\n\tnormalized_url TEXT NOT NULL, \n\tvideo_id TEXT NOT NULL, \n\tPRIMARY KEY (normalized_url), \n\tFOREIGN KEY(video_id) REFERENCES videos (video_id) ON DELETE CASCADE\n)"
    )
    op.execute(
        "CREATE TABLE discussion_states (\n\tstate_id UUID NOT NULL, \n\tvideo_id TEXT NOT NULL, \n\tsource_export_id UUID NOT NULL, \n\tschema_version TEXT NOT NULL, \n\thour_bucket TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcaptured_from TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcaptured_to TIMESTAMP WITH TIME ZONE NOT NULL, \n\texported_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcoverage JSONB NOT NULL, \n\ttitle TEXT, \n\tsource_metadata JSONB NOT NULL, \n\tlifecycle TEXT NOT NULL, \n\tPRIMARY KEY (state_id), \n\tCONSTRAINT uq_video_state UNIQUE (video_id, state_id), \n\tCONSTRAINT ck_state_lifecycle CHECK (lifecycle IN ('loading','ready','current','partial','failed')), \n\tCONSTRAINT ck_capture_times CHECK (captured_from <= captured_to AND captured_to <= exported_at), \n\tFOREIGN KEY(video_id) REFERENCES videos (video_id)\n)"
    )
    op.execute(
        "CREATE TABLE threads (\n\tstate_id UUID NOT NULL, \n\troot_id TEXT COLLATE \"C\" NOT NULL, \n\troot_author_uid TEXT, \n\troot_author_name TEXT, \n\tsource_title TEXT, \n\tcomment_count BIGINT NOT NULL CHECK (comment_count >= 0), \n\treply_count BIGINT NOT NULL CHECK (reply_count >= 0), \n\tparticipant_count BIGINT NOT NULL CHECK (participant_count >= 0), \n\tunknown_author_comment_count BIGINT NOT NULL CHECK (unknown_author_comment_count >= 0), \n\tcoverage JSONB NOT NULL, \n\tsource_metadata JSONB NOT NULL, \n\tPRIMARY KEY (state_id, root_id), \n\tCONSTRAINT ck_root_id_positive_id CHECK (root_id ~ '^[1-9][0-9]*$'), \n\tCONSTRAINT ck_root_author_uid_positive_id CHECK (root_author_uid ~ '^[1-9][0-9]*$'), \n\tFOREIGN KEY(state_id) REFERENCES discussion_states (state_id) ON DELETE CASCADE\n)"
    )
    op.execute(
        "CREATE TABLE comments (\n\tstate_id UUID NOT NULL, \n\tcomment_id TEXT COLLATE \"C\" NOT NULL, \n\troot_id TEXT COLLATE \"C\" NOT NULL, \n\tparent_id TEXT, \n\tkind TEXT NOT NULL, \n\tauthor_uid TEXT, \n\tnickname TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE, \n\tcollected_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tlike_count BIGINT CHECK (like_count >= 0), \n\treply_relation JSONB NOT NULL, \n\tcontent JSONB NOT NULL, \n\textra_fields JSONB NOT NULL, \n\troot_rank INTEGER GENERATED ALWAYS AS (CASE WHEN kind = 'root' THEN 0 ELSE 1 END) STORED, \n\tid_length INTEGER GENERATED ALWAYS AS (length(comment_id)) STORED, \n\tPRIMARY KEY (state_id, comment_id), \n\tCONSTRAINT fk_comment_thread FOREIGN KEY(state_id, root_id) REFERENCES threads (state_id, root_id) ON DELETE CASCADE, \n\tCONSTRAINT ck_comment_id_positive_id CHECK (comment_id ~ '^[1-9][0-9]*$'), \n\tCONSTRAINT ck_root_id_positive_id CHECK (root_id ~ '^[1-9][0-9]*$'), \n\tCONSTRAINT ck_parent_id_positive_id CHECK (parent_id ~ '^[1-9][0-9]*$'), \n\tCONSTRAINT ck_author_uid_positive_id CHECK (author_uid ~ '^[1-9][0-9]*$'), \n\tCONSTRAINT ck_comment_kind CHECK (kind IN ('root','reply')), \n\tCONSTRAINT ck_comment_root CHECK ((kind = 'root' AND root_id = comment_id AND parent_id IS NULL) OR (kind = 'reply' AND root_id <> comment_id)), \n\tCONSTRAINT ck_parent_not_self CHECK (parent_id IS NULL OR parent_id <> comment_id)\n)"
    )
    op.execute(
        "CREATE TABLE unclassified_comments (\n\tstate_id UUID NOT NULL, \n\tordinal BIGINT NOT NULL, \n\tpayload JSONB NOT NULL, \n\tPRIMARY KEY (state_id, ordinal), \n\tCONSTRAINT ck_unclassified_ordinal CHECK (ordinal >= 0), \n\tFOREIGN KEY(state_id) REFERENCES discussion_states (state_id) ON DELETE CASCADE\n)"
    )
    op.execute(
        "CREATE TABLE import_receipts (\n\tvideo_id TEXT NOT NULL, \n\tsource_export_id UUID NOT NULL, \n\tcanonical_digest TEXT NOT NULL, \n\tstate_id UUID, \n\tstatus TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcompleted_at TIMESTAMP WITH TIME ZONE, \n\tsafe_error_code TEXT, \n\tPRIMARY KEY (video_id, source_export_id), \n\tCONSTRAINT fk_receipt_state FOREIGN KEY(video_id, state_id) REFERENCES discussion_states (video_id, state_id), \n\tCONSTRAINT ck_receipt_status CHECK (status IN ('loading','ready','partial','published','failed','expired')), \n\tCONSTRAINT ck_receipt_digest CHECK (canonical_digest ~ '^[0-9a-f]{64}$'), \n\tFOREIGN KEY(video_id) REFERENCES videos (video_id)\n)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_one_working_state ON discussion_states (video_id) WHERE lifecycle <> 'current'"
    )
    op.execute(
        "CREATE INDEX ix_comments_thread_order ON comments (state_id, root_id, root_rank, created_at ASC NULLS LAST, id_length, comment_id)"
    )
    op.execute(
        "CREATE INDEX ix_comments_user_order ON comments (state_id, author_uid, created_at ASC NULLS LAST, id_length, comment_id)"
    )
    op.execute(
        "ALTER TABLE videos ADD CONSTRAINT fk_video_current_state_id FOREIGN KEY(video_id, current_state_id) REFERENCES discussion_states (video_id, state_id)"
    )
    op.execute(
        "ALTER TABLE videos ADD CONSTRAINT fk_video_working_state_id FOREIGN KEY(video_id, working_state_id) REFERENCES discussion_states (video_id, state_id)"
    )
    op.execute("COMMENT ON TABLE comments IS 'storage_codec=pg-text-v1'")


def downgrade():
    # This destructive reverse migration is supported only in disposable test schemas.
    schema = op.get_bind().execute(text("SELECT current_schema()")).scalar_one()
    if not re.fullmatch(r"bt_test_[0-9a-f]{32}", schema):
        raise RuntimeError("downgrade_requires_test_schema")
    op.drop_constraint("fk_video_current_state_id", "videos", type_="foreignkey")
    op.drop_constraint("fk_video_working_state_id", "videos", type_="foreignkey")
    op.drop_table("import_receipts")
    op.drop_table("unclassified_comments")
    op.drop_table("comments")
    op.drop_table("threads")
    op.drop_table("discussion_states")
    op.drop_table("video_links")
    op.drop_table("videos")
