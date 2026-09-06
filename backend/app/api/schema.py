"""Persistent HTTP API tables sharing the storage metadata."""

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Integer, Table, Text

from app.storage.schema import metadata

api_rate_limits = Table(
    "api_rate_limits",
    metadata,
    Column("client_key", Text, primary_key=True),
    Column("kind", Text, primary_key=True),
    Column("window_start", DateTime(timezone=True), primary_key=True),
    Column("hits", Integer, nullable=False),
    CheckConstraint("client_key ~ '^[0-9a-f]{64}$'", name="ck_api_rate_limit_client_key"),
    CheckConstraint("kind IN ('write','intent')", name="ck_api_rate_limit_kind"),
    CheckConstraint("hits >= 0", name="ck_api_rate_limit_hits"),
)
Index("ix_api_rate_limits_window_start", api_rate_limits.c.window_start)
