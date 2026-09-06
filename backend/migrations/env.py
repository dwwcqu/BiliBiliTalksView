"""Alembic environment; credentials remain outside configuration and logs."""

import os

from alembic import context

from app.api import schema as _api_schema  # noqa: F401 -- register API quota metadata
from app.jobs import schema as _job_schema  # noqa: F401 -- register queue metadata
from app.storage.connection import open_connection
from app.storage.schema import metadata

config = context.config


def run(connection):
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("offline_migration_not_supported")
elif config.attributes.get("connection") is not None:
    run(config.attributes["connection"])
else:
    with open_connection(os.environ["DATABASE_URL"]) as connection:
        run(connection)
