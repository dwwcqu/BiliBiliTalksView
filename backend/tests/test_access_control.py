"""Shared source policy tests; never contact Bilibili."""
import multiprocessing
from typing import ClassVar

import pytest

from app.comment_export.access_control import AccessControl, AccessControlError


class FakeClock:
    def __init__(self):
        self.value = 10000.0
        self.waits = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.value += seconds


def test_read_status_does_not_create_files(tmp_path):
    directory = tmp_path / "missing"
    assert AccessControl(directory).read_status()["cooldown_until"] is None
    assert not directory.exists()


def test_spacing_survives_restart(tmp_path):
    clock = FakeClock()
    first = AccessControl(tmp_path, clock.now, clock.sleep)
    with first.attempt("/x/test", {}):
        pass
    second = AccessControl(tmp_path, clock.now, clock.sleep)
    with second.attempt("/x/test", {}):
        assert clock.value == 10002
    assert clock.waits == [2]
    assert second.read_status()["next_allowed_at"] == 10004


def test_cooldown_prevents_attempt_and_survives_restart(tmp_path):
    clock = FakeClock()
    first = AccessControl(tmp_path, clock.now, clock.sleep)
    first.record_failure({"http_status": 429, "api_code": None, "retry_after_at": None})
    second = AccessControl(tmp_path, clock.now, clock.sleep)
    with pytest.raises(AccessControlError, match="cooldown_active"), second.attempt("/x/test", {}):
        pytest.fail("request must not run")
    assert second.read_status()["last_request_at"] is None
    assert second.read_status()["cooldown_until"] == 11800
    clock.value = 11800
    with second.attempt("/x/test", {}):
        pass


def test_business_failure_records_cooldown_and_releases(tmp_path):
    clock = FakeClock()
    control = AccessControl(tmp_path, clock.now, clock.sleep)

    class Refused(Exception):
        detail: ClassVar[dict] = {"http_status": 200, "api_code": -352, "retry_after_at": None}

    with pytest.raises(Refused), control.attempt("/x/test", {}):
        raise Refused()
    assert control.read_status()["cooldown_until"] == 11800
    clock.value = 11800
    with control.attempt("/x/test", {}):
        pass


def test_clock_rollback_and_interrupt_release(tmp_path):
    clock = FakeClock()
    control = AccessControl(tmp_path, clock.now, clock.sleep)
    with pytest.raises(KeyboardInterrupt), control.attempt("/x/test", {}):
        raise KeyboardInterrupt()
    clock.value -= 6
    with (pytest.raises(AccessControlError, match="clock_inconsistent"),
          control.attempt("/x/test", {})):
        pass
    clock.value = 10000
    with control.attempt("/x/test", {}):
        pass


def test_nested_attempt_is_busy_not_deadlocked(tmp_path):
    control = AccessControl(tmp_path)
    with (control.attempt("/x/test", {}),
          pytest.raises(AccessControlError, match="source_busy"),
          AccessControl(tmp_path).attempt("/x/test", {})):
        pass


def _hold_source(directory, entered, release):
    with AccessControl(directory).attempt("/x/test", {}):
        entered.set()
        release.wait(10)


def test_process_lock_releases_after_exit(tmp_path):
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    process = context.Process(target=_hold_source, args=(str(tmp_path), entered, release))
    process.start()
    try:
        assert entered.wait(10)
        with (pytest.raises(AccessControlError, match="source_busy"),
              AccessControl(tmp_path).attempt("/x/test", {})):
            pass
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0
    with AccessControl(tmp_path).attempt("/x/test", {}):
        pass


def test_request_exception_is_not_reclassified_as_control_error(tmp_path):
    control = AccessControl(tmp_path, _interval=0)
    error = OSError("local fake request failure")
    with pytest.raises(OSError) as caught, control.attempt("/x/test", {}):
        raise error
    assert caught.value is error
    with control.attempt("/x/test", {}):
        pass


def test_retry_after_and_non_access_failures(tmp_path):
    from datetime import UTC, datetime
    clock = FakeClock()
    control = AccessControl(tmp_path, clock.now, clock.sleep)
    for code in (401, 500, None):
        control.record_failure({"http_status": code, "api_code": None})
    assert not (tmp_path / "access.sqlite3").exists()
    deadline = datetime.fromtimestamp(15000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    control.record_failure({"http_status": 403, "retry_after_at": deadline})
    control.record_failure({"http_status": 412, "retry_after_at": "invalid"})
    assert control.read_status()["cooldown_until"] == 15000


def test_small_clock_rollback_waits_until_persisted_interval(tmp_path):
    clock = FakeClock()
    control = AccessControl(tmp_path, clock.now, clock.sleep)
    with control.attempt("/x/test", {}):
        pass
    clock.value -= 3
    with control.attempt("/x/test", {}):
        assert clock.value == 10002
    assert clock.waits == [5]


def test_killed_process_releases_file_lock(tmp_path):
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    process = context.Process(target=_hold_source, args=(str(tmp_path), entered, release))
    process.start()
    try:
        assert entered.wait(10)
        previous = AccessControl(tmp_path).read_status()["last_request_at"]
    finally:
        process.terminate()
        process.join(10)
    assert not process.is_alive()
    with AccessControl(tmp_path).attempt("/x/test", {}):
        assert AccessControl(tmp_path).read_status()["last_request_at"] >= previous + 2


def test_register_legacy_cooldown_without_source_failure(tmp_path):
    clock = FakeClock()
    control = AccessControl(tmp_path, clock.now, clock.sleep)
    status = control.register_legacy_cooldown()
    assert status["cooldown_until"] == 11800
    assert status["last_request_at"] is None
    assert status["next_allowed_at"] is None
    assert "http_status" not in status and "api_code" not in status
    clock.value -= 2
    assert control.register_legacy_cooldown()["cooldown_until"] == 11800
    assert AccessControl(tmp_path).read_status() == control.read_status()


def test_legacy_fixed_deadline_reapplication_is_idempotent(tmp_path):
    clock = FakeClock()
    control = AccessControl(tmp_path, clock.now, clock.sleep)
    assert control.register_legacy_cooldown(until=11800)["cooldown_until"] == 11800
    clock.value = 12000
    restarted = AccessControl(tmp_path, clock.now, clock.sleep)
    assert restarted.register_legacy_cooldown(until=11800)["cooldown_until"] == 11800
    assert restarted.register_legacy_cooldown(until=13000)["cooldown_until"] == 13000
    assert restarted.register_legacy_cooldown(until=11800)["cooldown_until"] == 13000


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), -float("inf"), "100", True])
def test_legacy_invalid_deadline_does_not_create_state(tmp_path, deadline):
    directory = tmp_path / "control"
    with pytest.raises(AccessControlError, match="invalid_cooldown_deadline"):
        AccessControl(directory).register_legacy_cooldown(until=deadline)
    assert not directory.exists()
