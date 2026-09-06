"""Dedicated connections: no pooling, reconnect, or credential logging."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from .errors import StorageError


@contextmanager
def open_connection(url: str) -> Iterator[Connection]:
    engine = None
    conn = None
    try:
        try:
            parsed = make_url(url)
            if parsed.drivername != "postgresql+psycopg":
                raise StorageError("unsupported_database_driver")
            engine = create_engine(parsed, poolclass=NullPool, echo=False, hide_parameters=True)
            conn = engine.connect()
        except (SQLAlchemyError, ValueError):
            raise StorageError("database_connection_failed") from None
        yield conn
    finally:
        if conn is not None:
            conn.close()
        if engine is not None:
            engine.dispose()
