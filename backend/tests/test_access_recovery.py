from copy import deepcopy

import httpx
import pytest

from app.comment_export.access_control import AccessClient, AccessControl
from app.comment_export.checkpoint import Checkpoint, read_control_snapshot
from app.comment_export.diagnostics import FailureDetail
from app.comment_export.recovery import diagnose, probe, recover


@pytest.fixture
def blocked_task(frozen_case, tmp_path):
    records, meta = deepcopy(frozen_case)
    meta["_unclassified"] = []
    meta["source"]["bvid"] = "BV1234567890"
    meta["input_url"] = "https://www.bilibili.com/video/BV1234567890"
    meta["_threads"] = {
        "100": {"pagination_status": "not_started", "count": None},
        "200": {"pagination_status": "verified", "count": 0},
    }
    task = tmp_path / "task"
    cp = Checkpoint(task / "work.sqlite3")
    cp.initialize(meta)
    cp.commit_page(
        "main:1",
        [r for r in records if r["kind"] == "root"],
        {"main_done": True, "requests": 3, "max_requests": 100},
    )
    progress = cp.get_progress()
    progress.update(
        blocked=True,
        stopped_reason="access_restricted",
        failure=FailureDetail(
            phase="replies",
            endpoint="/x/v2/reply/reply",
            http_status=403,
            category="access_restricted",
            video_id=meta["video_id"],
            target={"root_id": "100", "page": 1},
            checkpoint_revision=progress["checkpoint_revision"],
            safe_reason="access_restricted",
        ).to_dict(),
    )
    cp.set_progress(progress)
    cp.close()
    return task


@pytest.fixture
def success_client(tmp_path):
    def respond(request):
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"aid": 10001, "bvid": "BV1234567890", "title": "test"}},
            )
        assert request.url.path == "/x/v2/reply/reply"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "page": {"num": 1, "size": 20, "count": 0},
                    "replies": [],
                    "root": {
                        "rpid": 100,
                        "root": 0,
                        "member": {"mid": 1, "uname": "甲/乙"},
                        "content": {"message": "一\n二"},
                        "ctime": 1,
                        "like": 0,
                    },
                },
            },
        )

    with AccessClient(
        transport=httpx.MockTransport(respond),
        access_policy=AccessControl(tmp_path / "control", _interval=0),
    ) as client:
        yield client


def test_diagnose_is_readonly(blocked_task, tmp_path):
    path = blocked_task / "work.sqlite3"
    before = path.read_bytes()
    result = diagnose(blocked_task, tmp_path / "absent-control")
    assert result["blocked"] is True
    assert result["failure"]["http_status"] == 403
    assert path.read_bytes() == before
    assert not (tmp_path / "absent-control").exists()


def test_probe_does_not_unblock_or_commit(blocked_task, success_client):
    before = read_control_snapshot(blocked_task)
    result = probe(blocked_task, success_client)
    after = read_control_snapshot(blocked_task)
    assert result["probe_ok"]
    assert after["progress"]["blocked"] is True
    assert after["progress"]["checkpoint_revision"] == before["progress"]["checkpoint_revision"]
    assert after["comment_count"] == before["comment_count"]
    assert after["progress"]["requests"] == before["progress"]["requests"] + 1


def test_recover_commits_before_unblocking(blocked_task, success_client, tmp_path):
    result = recover(blocked_task, success_client, tmp_path / "output")
    after = read_control_snapshot(blocked_task)
    assert result["collection"]["finished"]
    assert after["progress"]["blocked"] is False
    assert after["progress"]["requests"] == 6
    assert after["progress"]["checkpoint_revision"] == 2


def test_successful_probe_is_not_a_resume_ticket(blocked_task, success_client):
    from app.comment_export.collector import collect
    from app.comment_export.source import CollectionStopped

    probe(blocked_task, success_client)
    with pytest.raises(CollectionStopped, match="blocked_requires_revalidation"):
        collect(
            "https://www.bilibili.com/video/BV1234567890",
            blocked_task,
            success_client,
            100,
            resume=True,
        )


def test_failed_first_page_transaction_keeps_blocked(
    blocked_task, success_client, tmp_path, monkeypatch
):
    import sqlite3

    original = Checkpoint.commit_page

    def fail_reply(cp, key, comments, progress):
        if key.startswith("reply:"):
            raise sqlite3.IntegrityError("injected")
        return original(cp, key, comments, progress)

    monkeypatch.setattr(Checkpoint, "commit_page", fail_reply)
    result = recover(blocked_task, success_client, tmp_path / "output")
    snapshot = read_control_snapshot(blocked_task)
    assert not result["collection"]["finished"]
    assert snapshot["progress"]["blocked"] is True
    assert snapshot["progress"]["checkpoint_revision"] == 1
    assert snapshot["comment_count"] == 2
    assert snapshot["progress"]["requests"] == 6


def test_legacy_registration_sets_cooldown_without_requests(blocked_task, success_client):
    from app.comment_export.access_control import AccessControlError

    cp = Checkpoint(blocked_task / "work.sqlite3")
    progress = cp.get_progress()
    progress.pop("failure")
    cp.set_progress(progress)
    cp.close()
    with pytest.raises(AccessControlError, match="cooldown_active"):
        probe(blocked_task, success_client, next_pending=True)
    deadline = success_client.access_policy.read_status()["cooldown_until"]
    with pytest.raises(AccessControlError, match="cooldown_active"):
        probe(blocked_task, success_client, next_pending=True)
    assert success_client.access_policy.read_status()["cooldown_until"] == deadline
    assert read_control_snapshot(blocked_task)["progress"]["requests"] == 3


def test_completed_thread_cannot_be_used_for_recovery_proof(blocked_task, success_client):
    from app.comment_export.source import CollectionStopped

    cp = Checkpoint(blocked_task / "work.sqlite3")
    progress = cp.get_progress()
    progress["failure"]["target"]["root_id"] = "200"
    cp.set_progress(progress)
    cp.close()
    with pytest.raises(CollectionStopped, match="failure_target_stale"):
        probe(blocked_task, success_client)


@pytest.mark.parametrize("temporary_reason", ["source_busy", "cooldown_active"])
def test_registered_legacy_target_survives_control_pause(blocked_task, temporary_reason):
    from app.comment_export.recovery import select_target

    cp = Checkpoint(blocked_task / "work.sqlite3")
    progress = cp.get_progress()
    progress["failure"]["category"] = "unknown_legacy"
    progress["legacy_registered_at"] = "2026-09-06T00:00:00Z"
    progress["stopped_reason"] = temporary_reason
    cp.set_progress(progress)
    cp.close()
    assert select_target(read_control_snapshot(blocked_task), True) == {
        "phase": "replies",
        "root_id": "100",
        "page": 1,
    }


def test_changed_credentials_invalidate_ephemeral_proof(blocked_task, success_client):
    from app.comment_export.collector import collect
    from app.comment_export.recovery import _probe_locked
    from app.comment_export.source import CollectionStopped

    _, proof = _probe_locked(blocked_task, success_client, False)
    success_client.headers["Cookie"] = "changed-test-credential"
    before = read_control_snapshot(blocked_task)["progress"]["requests"]
    with pytest.raises(CollectionStopped, match="invalid_recovery_proof"):
        collect(
            "https://www.bilibili.com/video/BV1234567890",
            blocked_task,
            success_client,
            100,
            resume=True,
            recovery_proof=proof,
        )
    assert read_control_snapshot(blocked_task)["progress"]["requests"] == before


def test_legacy_registration_crash_preserves_deadline(blocked_task, success_client, monkeypatch):
    from app.comment_export.access_control import AccessControlError

    cp = Checkpoint(blocked_task / "work.sqlite3")
    progress = cp.get_progress()
    progress.pop("failure")
    cp.set_progress(progress)
    cp.close()
    control = success_client.access_policy
    original = control.register_legacy_cooldown

    def crash(**kwargs):
        raise OSError("injected registration interruption")

    monkeypatch.setattr(control, "register_legacy_cooldown", crash)
    with pytest.raises(OSError):
        probe(blocked_task, success_client, next_pending=True)
    deadline = read_control_snapshot(blocked_task)["progress"]["legacy_cooldown_until"]
    monkeypatch.setattr(control, "register_legacy_cooldown", original)
    with pytest.raises(AccessControlError, match="cooldown_active"):
        probe(blocked_task, success_client, next_pending=True)
    assert control.read_status()["cooldown_until"] == deadline


def test_cookie_jar_change_invalidates_proof(blocked_task, success_client):
    from app.comment_export.collector import collect
    from app.comment_export.recovery import _probe_locked
    from app.comment_export.source import CollectionStopped
    _, proof = _probe_locked(blocked_task, success_client, False)
    success_client.cookies.set("SESSDATA", "changed-jar-test", domain="api.bilibili.com", path="/")
    with pytest.raises(CollectionStopped, match="invalid_recovery_proof"):
        collect("https://www.bilibili.com/video/BV1234567890", blocked_task, success_client,
                100, resume=True, recovery_proof=proof)


def test_recover_retains_unavailable_floor_and_finishes_other_work(blocked_task, tmp_path):
    def response(request):
        if request.url.path == "/x/v2/reply/reply":
            return httpx.Response(200, json={"code": 12022, "message": "not available"})
        return httpx.Response(200, json={"code": 0, "data": {"aid": 10001, "bvid": "BV1234567890", "title": "test"}})
    with AccessClient(transport=httpx.MockTransport(response),
                      access_policy=AccessControl(tmp_path / "control", _interval=0)) as client:
        result = recover(blocked_task, client, tmp_path / "output")
        assert client.access_policy.read_status()["cooldown_until"] is None
    snapshot = read_control_snapshot(blocked_task)
    assert result["collection"]["finished"]
    assert not result["collection"]["blocked"]
    assert result["collection"]["coverage"]["status"] == "partial"
    assert snapshot["comment_count"] == 2
    assert snapshot["metadata"]["_threads"]["100"]["unavailable"]["api_code"] == 12022
    assert snapshot["metadata"]["_threads"]["100"]["pagination_status"] == "partial"


def test_http_denial_never_becomes_unavailable_skip(blocked_task, tmp_path):
    from app.comment_export.source import CollectionStopped
    with AccessClient(transport=httpx.MockTransport(lambda request: httpx.Response(429, json={"code": 12022})),
                      access_policy=AccessControl(tmp_path / "control", _interval=0)) as client:
        with pytest.raises(CollectionStopped):
            recover(blocked_task, client, tmp_path / "output")
        assert client.access_policy.read_status()["cooldown_until"] is not None
    snapshot = read_control_snapshot(blocked_task)
    assert snapshot["progress"]["blocked"]
    assert not snapshot["metadata"]["_threads"]["100"].get("unavailable")
    assert snapshot["comment_count"] == 2
