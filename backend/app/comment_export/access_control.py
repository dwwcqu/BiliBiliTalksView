"""Persisted, per-deployment source spacing and bounded access cooldown."""

import math
import os
import sqlite3
import threading
import time
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime
from pathlib import Path

import httpx

from .contract import ContractError
from .diagnostics import is_reply_unavailable
from .publication import exclusive_lock


class AccessControlError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


_registry_guard = threading.Lock()
_registry: dict[str, threading.Lock] = {}


def _local_lock(directory: Path):
    key = str(directory.resolve()).casefold() if os.name == "nt" else str(directory.resolve())
    with _registry_guard:
        return _registry.setdefault(key, threading.Lock())


class AccessControl:
    def __init__(self, directory, clock=time.time, sleep=time.sleep, *, _interval=2.0):
        if not math.isfinite(_interval) or _interval < 0:
            raise ValueError("invalid_interval")
        self.directory = Path(directory)
        self.path = self.directory / "access.sqlite3"
        self.clock = clock
        self.sleep = sleep
        self._interval = _interval
        self._owner = threading.local()

    @contextmanager
    def _locked(self):
        local = _local_lock(self.directory)
        if not local.acquire(blocking=False):
            raise AccessControlError("source_busy")
        try:
            with ExitStack() as stack:
                try:
                    stack.enter_context(exclusive_lock(self.directory / "source.lock"))
                except ContractError as exc:
                    raise AccessControlError("source_busy") from exc
                except OSError as exc:
                    raise AccessControlError("control_state_invalid") from exc
                self._owner.active = True
                try:
                    yield
                finally:
                    self._owner.active = False
        finally:
            local.release()

    def _load(self, create=False):
        if not self.path.exists() and not create:
            return {
                "last_request_at": None,
                "next_allowed_at": None,
                "cooldown_until": None,
                "last_clock_at": None,
            }
        uri = self.path.resolve().as_uri() + "?mode=" + ("rwc" if create else "ro")
        try:
            with closing(sqlite3.connect(uri, uri=True)) as db, db:
                if create:
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS access_state (id INTEGER PRIMARY KEY "
                        "CHECK(id=1), last_request_at REAL, next_allowed_at REAL, "
                        "cooldown_until REAL, last_clock_at REAL)"
                    )
                    db.execute("INSERT OR IGNORE INTO access_state(id) VALUES(1)")
                row = db.execute(
                    "SELECT last_request_at,next_allowed_at,cooldown_until,"
                    "last_clock_at FROM access_state WHERE id=1"
                ).fetchone()
            if row is None or any(
                value is not None
                and (not isinstance(value, (int, float)) or not math.isfinite(value))
                for value in row
            ):
                raise AccessControlError("control_state_invalid")
            return dict(
                zip(
                    ("last_request_at", "next_allowed_at", "cooldown_until", "last_clock_at"),
                    row,
                    strict=True,
                )
            )
        except (sqlite3.Error, OSError) as exc:
            raise AccessControlError("control_state_invalid") from exc

    def _save(self, state):
        try:
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute(
                    "UPDATE access_state SET last_request_at=?,next_allowed_at=?,"
                    "cooldown_until=?,last_clock_at=? WHERE id=1",
                    tuple(
                        state[key]
                        for key in (
                            "last_request_at",
                            "next_allowed_at",
                            "cooldown_until",
                            "last_clock_at",
                        )
                    ),
                )
        except (sqlite3.Error, OSError) as exc:
            raise AccessControlError("control_state_invalid") from exc

    def _now(self, state):
        now = self.clock()
        if not isinstance(now, (int, float)) or not math.isfinite(now):
            raise AccessControlError("clock_inconsistent")
        previous = state["last_clock_at"]
        if previous is not None and now < previous - 5:
            raise AccessControlError("clock_inconsistent")
        return now

    def read_status(self):
        """Read only: absent state remains absent, including its parent directory."""
        return self._load()

    @contextmanager
    def attempt(self, endpoint, target):
        # endpoint/target belong to task diagnostics, not this shared source database.
        with self._locked():
            state = self._load(create=True)
            now = self._now(state)
            if state["cooldown_until"] is not None and now < state["cooldown_until"]:
                raise AccessControlError("cooldown_active")
            while state["next_allowed_at"] is not None and now < state["next_allowed_at"]:
                self.sleep(state["next_allowed_at"] - now)
                now = self._now(state)
            state["last_request_at"] = now
            state["next_allowed_at"] = now + self._interval
            state["last_clock_at"] = max(now, state["last_clock_at"] or now)
            self._save(state)
            try:
                yield
            except BaseException as exc:
                detail = getattr(exc, "detail", None)
                if detail is not None:
                    self.record_failure(detail)
                raise

    def register_legacy_cooldown(self, *, until: float | None = None):
        """Apply local protection without inventing a historical source response.

        The task checkpoint persists a fixed deadline before applying it here.
        Reapplying that deadline never starts a new cooldown after a crash.
        """
        if until is not None and (type(until) not in (int, float) or not math.isfinite(until)):
            raise AccessControlError("invalid_cooldown_deadline")
        with self._locked():
            state = self._load(create=True)
            now = self._now(state)
            deadline = now + 1800 if until is None else until
            existing = state["cooldown_until"]
            state["cooldown_until"] = deadline if existing is None else max(existing, deadline)
            state["last_clock_at"] = max(now, state["last_clock_at"] or now)
            self._save(state)
            return state

    def record_failure(self, detail):
        value = detail.to_dict() if hasattr(detail, "to_dict") else detail
        if not isinstance(value, dict):
            raise AccessControlError("control_state_invalid")
        if is_reply_unavailable(value):
            return
        code = value.get("api_code")
        if value.get("http_status") not in {429, 403, 412} and not (
            type(code) is int and code != 0
        ):
            return
        if getattr(self._owner, "active", False):
            self._failure_locked(value)
        else:
            with self._locked():
                self._failure_locked(value)

    def _failure_locked(self, value):
        state = self._load(create=True)
        now = self._now(state)
        retry = value.get("retry_after_at")
        retry_at = 0.0
        if isinstance(retry, str):
            try:
                stamp = datetime.fromisoformat(retry)
                if stamp.tzinfo is not None:
                    retry_at = stamp.timestamp()
            except (ValueError, OverflowError):
                pass
        state["cooldown_until"] = max(state["cooldown_until"] or 0, now + 1800, retry_at)
        state["last_clock_at"] = max(now, state["last_clock_at"] or now)
        self._save(state)


class AccessClient(httpx.Client):
    def __init__(self, *args, access_policy: AccessControl, **kwargs):
        super().__init__(*args, **kwargs)
        self.access_policy = access_policy
