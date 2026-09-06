"""Atomically publish one verified state and remove obsolete state bodies."""

from contextlib import nullcontext
from datetime import UTC, datetime

from sqlalchemy import delete, select, update

from .codec import decode_json
from .errors import StorageError
from .locks import video_lock
from .payloads import prune_payloads
from .schema import discussion_states as states
from .schema import import_receipts as receipts
from .schema import videos
from .semantic import from_state


def check_freshness(candidate, previous, equivalent=False) -> None:
    if previous is None:
        return
    if (
        candidate["hour_bucket"] < previous["hour_bucket"]
        or candidate["captured_from"] < previous["captured_from"]
        or candidate["captured_to"] < previous["captured_to"]
    ):
        raise StorageError("stale_batch")
    if (
        candidate["hour_bucket"] == previous["hour_bucket"]
        and candidate["captured_to"] == previous["captured_to"]
        and candidate["source_export_id"] != previous["source_export_id"]
        and not equivalent
    ):
        # Equal-time replacements need content reconciliation, never arbitrary ordering.
        raise StorageError("ambiguous_replacement")


def discard_state(conn, state) -> None:
    receipt_status = "failed" if state["lifecycle"] in {"failed", "loading"} else "expired"
    conn.execute(
        update(receipts)
        .where(receipts.c.state_id == state["state_id"])
        .values(
            state_id=None,
            status=receipt_status,
            safe_error_code="interrupted" if state["lifecycle"] == "loading" else None,
        )
    )
    conn.execute(
        update(videos)
        .where(videos.c.working_state_id == state["state_id"])
        .values(working_state_id=None)
    )
    conn.execute(delete(states).where(states.c.state_id == state["state_id"]))


def publish_locked(conn, video_id: str, state_id: str, *, atomic: bool = False) -> dict:
    if atomic and not conn.in_transaction():
        raise StorageError("atomic_transaction_required")
    with (nullcontext() if atomic else conn.begin()):
        video = (
            conn.execute(select(videos).where(videos.c.video_id == video_id).with_for_update())
            .mappings()
            .one()
        )
        state = (
            conn.execute(
                select(states).where(states.c.state_id == state_id, states.c.video_id == video_id)
            )
            .mappings()
            .one_or_none()
        )
        if not state:
            raise StorageError("state_expired")
        if video["current_state_id"] == state_id and state["lifecycle"] == "current":
            return {
                "state_id": state_id,
                "receipt_status": "published",
                "published": True,
                "coverage": decode_json(state["coverage"]),
            }
        if state["lifecycle"] == "partial":
            return {
                "state_id": state_id,
                "receipt_status": "partial",
                "published": False,
                "coverage": decode_json(state["coverage"]),
            }
        if (
            video["working_state_id"] != state_id
            or state["lifecycle"] != "ready"
            or state["coverage"]["status"] != "verified"
        ):
            raise StorageError("state_not_publishable")
        old = None
        if video["current_state_id"]:
            old = (
                conn.execute(select(states).where(states.c.state_id == video["current_state_id"]))
                .mappings()
                .one()
            )
            check_freshness(state, old, equivalent=from_state(conn, state) == from_state(conn, old))
            conn.execute(
                update(receipts)
                .where(receipts.c.state_id == old["state_id"])
                .values(state_id=None, status="expired")
            )
        conn.execute(
            update(videos)
            .where(videos.c.video_id == video_id)
            .values(current_state_id=state_id, working_state_id=None)
        )
        if old:
            conn.execute(delete(states).where(states.c.state_id == old["state_id"]))
        prune_payloads(conn, video_id)
        conn.execute(
            update(states).where(states.c.state_id == state_id).values(lifecycle="current")
        )
        conn.execute(
            update(receipts)
            .where(receipts.c.state_id == state_id)
            .values(status="published", completed_at=datetime.now(UTC))
        )
        return {
            "state_id": state_id,
            "receipt_status": "published",
            "published": True,
            "coverage": decode_json(state["coverage"]),
        }


def publish_state(conn, video_id: str, state_id: str) -> dict:
    with video_lock(conn, video_id):
        return publish_locked(conn, video_id, state_id)
