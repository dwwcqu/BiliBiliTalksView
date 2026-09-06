"""Integration tests use only an explicitly configured disposable PostgreSQL database."""

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.storage.schema import discussion_states, videos


@pytest.fixture
def migration_config():
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


@pytest.fixture
def db_engine(migration_config):
    raw_url = os.environ.get("TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("TEST_DATABASE_URL required for PostgreSQL integration tests")
    url = make_url(raw_url)
    if url.drivername != "postgresql+psycopg" or url.database != "bilibili_talks_test":
        pytest.fail("test_database_must_be_bilibili_talks_test")
    admin = create_engine(url, poolclass=NullPool, hide_parameters=True)
    schema = "bt_test_" + uuid4().hex
    engine = None
    try:
        with admin.begin() as connection:
            if (
                connection.execute(text("SELECT current_database()")).scalar_one()
                != "bilibili_talks_test"
            ):
                pytest.fail("unexpected_database")
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(
            url,
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={"options": f"-csearch_path={schema}"},
        )
        with engine.begin() as connection:
            migration_config.attributes["connection"] = connection
            command.upgrade(migration_config, "head")
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        # Only the exact schema created above can be cleaned; never a database or public schema.
        if re.fullmatch(r"bt_test_[0-9a-f]{32}", schema):
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def db(db_engine):
    with db_engine.connect() as connection:
        yield connection


@pytest.fixture
def conn(db):
    return db


@pytest.fixture
def state_factory():
    def create(connection, video_id="bilibili:video:1", lifecycle="loading", **overrides):
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        aid = video_id.rsplit(":", 1)[-1]
        connection.execute(
            pg_insert(videos)
            .values(video_id=video_id, aid=aid, oid=aid, platform="bilibili", comment_type=1)
            .on_conflict_do_nothing()
        )
        stamp = datetime(2026, 9, 6, tzinfo=UTC)
        values = {
            "state_id": str(uuid4()),
            "video_id": video_id,
            "lifecycle": lifecycle,
            "source_export_id": str(uuid4()),
            "schema_version": "1.0.0",
            "hour_bucket": stamp,
            "captured_from": stamp,
            "captured_to": stamp,
            "exported_at": stamp,
            "coverage": {"status": "partial"},
            "source_metadata": {},
        }
        values.update(overrides)
        connection.execute(insert(discussion_states).values(**values))
        return values

    return create
