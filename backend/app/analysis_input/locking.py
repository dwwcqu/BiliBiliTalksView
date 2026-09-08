"""Bounded acquisition of the existing cross-platform OS file lock."""
import time
from contextlib import contextmanager
from pathlib import Path

from app.comment_export.contract import ContractError
from app.comment_export.publication import exclusive_lock

from .errors import InputPreparationError


@contextmanager
def preparation_lock(path: Path, *, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while True:
        lock = exclusive_lock(path)
        try:
            lock.__enter__()
            break
        except ContractError as exc:
            if str(exc) != "busy":
                raise
            if time.monotonic() >= deadline:
                raise InputPreparationError("lock_timeout") from exc
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    try:
        yield
    finally:
        lock.__exit__(None, None, None)
