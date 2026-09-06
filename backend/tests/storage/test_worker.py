from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select


def fake_source(monkeypatch, tmp_path):
    from app.comment_export import collector
    from app.comment_export.access_control import AccessClient, AccessControl
    from app.jobs import runner

    resolved = {
        "source": {
            "platform": "bilibili",
            "aid": "1",
            "oid": "1",
            "bvid": "BV1234567890",
            "episode_id": "1",
            "comment_type": 1,
        },
        "canonical_url": "https://www.bilibili.com/video/BV1234567890",
        "title": "sample",
    }
    monkeypatch.setattr(runner, "resolve_video", lambda *_: resolved)
    monkeypatch.setattr(collector, "resolve_video", lambda *_: resolved)
    monkeypatch.setattr(collector, "get_signing_keys", lambda *_: ("a", "b"))
    root = {
        "rpid": 100,
        "root": 0,
        "parent": 0,
        "member": {"mid": 1, "uname": "user"},
        "content": {"message": "root"},
        "ctime": 1,
        "like": 0,
        "rcount": 0,
    }
    monkeypatch.setattr(
        collector,
        "fetch_main",
        lambda *_: {"replies": [root], "cursor": {"is_end": True, "all_count": 1}},
    )
    monkeypatch.setattr(
        collector,
        "fetch_replies",
        lambda *_: {"root": root, "page": {"num": 1, "size": 20, "count": 0}, "replies": []},
    )
    return lambda: AccessClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
        access_policy=AccessControl(tmp_path / "control"),
    )


def test_worker_resolves_collects_and_cache_submit_stays_local(
    conn, db_engine, tmp_path, monkeypatch
):
    from app.jobs.repository import get_job, submit
    from app.jobs.runner import Worker
    from app.storage.schema import comments

    factory = fake_source(monkeypatch, tmp_path)
    request = submit(
        conn, "https://www.bilibili.com/bangumi/play/ep1/", datetime(2026, 9, 5, 8, 59, tzinfo=UTC)
    )
    assert request["disposition"] == "request"
    with Worker(db_engine, tmp_path / "jobs", factory) as worker:
        worker.run_once()
        result = worker.run_once()
        assert result["status"] == "succeeded", get_job(conn, result["job_id"])["safe_error"]
        job = get_job(conn, result["job_id"])
        assert job["hour_bucket"] == datetime(2026, 9, 5, 8, tzinfo=UTC)
        assert worker.run_once()["status"] == "idle"
    assert submit(conn, "https://www.bilibili.com/video/BV1234567890")["disposition"] == "cache"
    with conn.begin():
        assert conn.scalar(select(func.count()).select_from(comments)) == 1


def test_second_worker_cannot_run_while_first_owns_session(db_engine, tmp_path):
    from app.jobs.errors import JobError
    from app.jobs.runner import Worker

    with (
        Worker(db_engine, tmp_path / "jobs"),
        pytest.raises(JobError, match="worker_busy"),
        Worker(db_engine, tmp_path / "jobs"),
    ):
        pytest.fail("second worker acquired singleton")


def test_cancellation_at_page_boundary_prevents_publication(conn, db_engine, tmp_path, monkeypatch):
    from app.comment_export import collector
    from app.jobs.repository import control_job, get_request, submit
    from app.jobs.runner import Worker
    from app.storage.schema import discussion_states

    factory = fake_source(monkeypatch, tmp_path)
    request = submit(conn, "https://www.bilibili.com/bangumi/play/ep1")
    with Worker(db_engine, tmp_path / "jobs", factory) as worker:
        worker.run_once()
        job_id = get_request(conn, request["request_id"])["job_id"]
        original = collector.fetch_main

        def cancel(*args):
            with db_engine.connect() as operator:
                control_job(operator, job_id, "cancel")
            return original(*args)

        monkeypatch.setattr(collector, "fetch_main", cancel)
        assert worker.run_once()["status"] == "cancelled"
    with conn.begin():
        assert conn.scalar(select(func.count()).select_from(discussion_states)) == 0


def test_resolution_network_retries_are_bounded(conn, db_engine, tmp_path, monkeypatch):
    from datetime import timedelta

    from sqlalchemy import update

    from app.comment_export.source import CollectionStopped
    from app.jobs import runner
    from app.jobs.repository import get_request, submit
    from app.jobs.runner import Worker
    from app.jobs.schema import resolution_requests

    factory = fake_source(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runner,
        "resolve_video",
        lambda *_: (_ for _ in ()).throw(CollectionStopped("network_error")),
    )
    request = submit(conn, "https://www.bilibili.com/bangumi/play/ep1")
    with Worker(db_engine, tmp_path / "jobs", factory) as worker:
        for expected in ("waiting_source", "waiting_source", "failed"):
            assert worker.run_once()["status"] == expected
            with conn.begin():
                conn.execute(
                    update(resolution_requests).values(
                        not_before=datetime.now(UTC) - timedelta(seconds=1)
                    )
                )
    row = get_request(conn, request["request_id"])
    assert row["retry_count"] == 2
    assert (
        submit(conn, "https://www.bilibili.com/bangumi/play/ep1")["request_id"]
        == request["request_id"]
    )


def test_request_budget_and_lost_owner_stop_execution(conn, db_engine, tmp_path):
    from sqlalchemy import update

    from app.comment_export.source import CollectionStopped
    from app.jobs.dispatch import claim_next
    from app.jobs.ownership import TaskLease
    from app.jobs.repository import get_request, submit
    from app.jobs.runner import Worker
    from app.jobs.schema import source_runtime

    request = submit(conn, "https://www.bilibili.com/bangumi/play/ep1")
    with Worker(db_engine, tmp_path / "jobs") as worker:
        claim = claim_next(worker.owner)
        lease = TaskLease(worker.owner, claim)
        for _ in range(6):
            lease.before_request(httpx.Request("GET", "https://api.bilibili.com/test"))
        with pytest.raises(CollectionStopped, match="budget_exhausted"):
            lease.before_request(httpx.Request("GET", "https://api.bilibili.com/test"))
        assert get_request(conn, request["request_id"])["requests"] == 6
        with conn.begin():
            conn.execute(update(source_runtime).values(worker_token=None))
        with pytest.raises(CollectionStopped, match="lease_lost"):
            lease.check()


def test_source_gate_blocks_other_jobs_until_explicit_recovery(
    conn, db_engine, tmp_path, monkeypatch
):
    from app.comment_export import collector, recovery
    from app.comment_export.diagnostics import FailureDetail
    from app.comment_export.source import CollectionStopped
    from app.jobs.repository import control_job, get_request, submit
    from app.jobs.runner import Worker
    from app.jobs.schema import source_runtime

    factory = fake_source(monkeypatch, tmp_path)
    first = submit(conn, "https://www.bilibili.com/bangumi/play/ep1")
    healthy = collector.fetch_replies

    def denied(*_):
        raise CollectionStopped(
            "access_restricted",
            FailureDetail(
                endpoint="/x/v2/reply/reply",
                phase="replies",
                http_status=403,
                category="access_restricted",
                video_id="bilibili:video:1",
                target={"root_id": "100", "page": 1},
                safe_reason="access_restricted",
            ),
        )

    with Worker(db_engine, tmp_path / "jobs", factory) as worker:
        worker.run_once()
        monkeypatch.setattr(collector, "fetch_replies", denied)
        result = worker.run_once()
        assert result["status"] == "blocked"
        submit(conn, "https://www.bilibili.com/bangumi/play/ep2")
        assert worker.run_once()["status"] == "idle"
        with conn.begin():
            assert conn.scalar(select(source_runtime.c.source_gate)) == "needs_operator"
        monkeypatch.setattr(collector, "fetch_replies", healthy)
        monkeypatch.setattr(recovery, "fetch_replies", healthy)
        control_job(conn, get_request(conn, first["request_id"])["job_id"], "recover")
        assert worker.run_once()["status"] == "succeeded"
        with conn.begin():
            assert conn.scalar(select(source_runtime.c.source_gate)) == "normal"


def test_restarted_worker_resumes_same_checkpoint_after_owner_loss(
    conn, db_engine, tmp_path, monkeypatch
):
    from datetime import timedelta

    from sqlalchemy import update

    from app.comment_export import collector
    from app.comment_export.source import CollectionStopped
    from app.jobs.repository import get_request, submit
    from app.jobs.runner import Worker
    from app.jobs.schema import collection_jobs, source_runtime

    factory = fake_source(monkeypatch, tmp_path)
    request = submit(conn, "https://www.bilibili.com/bangumi/play/ep1")
    main_calls = []
    original_main = collector.fetch_main
    monkeypatch.setattr(
        collector, "fetch_main", lambda *args: main_calls.append(1) or original_main(*args)
    )
    healthy = collector.fetch_replies

    def lose_owner(*_):
        with db_engine.begin() as operator:
            operator.execute(update(source_runtime).values(worker_token=None))
        raise CollectionStopped("lease_lost")

    with Worker(db_engine, tmp_path / "jobs", factory) as worker:
        worker.run_once()
        monkeypatch.setattr(collector, "fetch_replies", lose_owner)
        assert worker.run_once()["status"] == "interrupted"
    job_id = get_request(conn, request["request_id"])["job_id"]
    with conn.begin():
        conn.execute(
            update(collection_jobs)
            .where(collection_jobs.c.job_id == job_id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    monkeypatch.setattr(collector, "fetch_replies", healthy)
    with Worker(db_engine, tmp_path / "jobs", factory) as restarted:
        result = restarted.run_once()
    assert result == {"status": "succeeded", "job_id": job_id}
    assert main_calls == [1]


def test_heartbeat_checks_actual_session_lock(conn, db_engine, tmp_path):
    from app.jobs.dispatch import claim_next
    from app.jobs.errors import JobError
    from app.jobs.ownership import TaskLease
    from app.jobs.repository import submit
    from app.jobs.runner import Worker

    submit(conn, "https://www.bilibili.com/bangumi/play/ep1")
    with Worker(db_engine, tmp_path / "jobs") as worker:
        claim = claim_next(worker.owner)
        lease = TaskLease(worker.owner, claim)
        lease.renew()
        worker.owner.connection.invalidate()
        with pytest.raises(JobError, match="lease_lost"):
            lease.renew()


def test_cancelled_blocker_requires_revalidation_without_restarting(
    conn, db_engine, tmp_path, monkeypatch
):
    from app.comment_export import collector, recovery
    from app.comment_export.diagnostics import FailureDetail
    from app.comment_export.source import CollectionStopped
    from app.jobs.repository import control_job, get_job, request_revalidation, submit
    from app.jobs.runner import Worker
    from app.jobs.schema import source_runtime

    factory = fake_source(monkeypatch, tmp_path)
    submit(conn, "https://www.bilibili.com/bangumi/play/ep1")
    healthy = collector.fetch_replies

    def denied(*_):
        raise CollectionStopped(
            "access_restricted",
            FailureDetail(
                endpoint="/x/v2/reply/reply",
                phase="replies",
                http_status=403,
                category="access_restricted",
                video_id="bilibili:video:1",
                target={"root_id": "100", "page": 1},
                safe_reason="access_restricted",
            ),
        )

    with Worker(db_engine, tmp_path / "jobs", factory) as worker:
        worker.run_once()
        monkeypatch.setattr(collector, "fetch_replies", denied)
        blocked = worker.run_once()
        control_job(conn, blocked["job_id"], "cancel")
        assert worker.run_once()["status"] == "idle"
        with conn.begin():
            assert conn.scalar(select(source_runtime.c.source_gate)) == "needs_operator"
        monkeypatch.setattr(recovery, "fetch_replies", healthy)
        request_revalidation(conn)
        assert worker.run_once()["status"] == "source_ready"
        assert get_job(conn, blocked["job_id"])["status"] == "cancelled"


def test_cancel_race_still_records_source_restriction(conn, db_engine, tmp_path, monkeypatch):
    from app.jobs.dispatch import claim_next, defer
    from app.jobs.repository import control_job, get_request, submit
    from app.jobs.runner import Worker
    from app.jobs.schema import source_runtime

    factory = fake_source(monkeypatch, tmp_path)
    request = submit(conn, "https://www.bilibili.com/bangumi/play/ep1")
    with Worker(db_engine, tmp_path / "jobs", factory) as worker:
        worker.run_once()
        claim = claim_next(worker.owner)
        job_id = get_request(conn, request["request_id"])["job_id"]
        control_job(conn, job_id, "cancel")
        assert defer(worker.owner, claim, "access_restricted", source_block=True)
        with conn.begin():
            runtime = conn.execute(select(source_runtime)).mappings().one()
        assert runtime["source_gate"] == "needs_operator"
        assert runtime["blocked_id"] == job_id


def test_resolution_commit_rechecks_owner_in_transaction(conn, db_engine, tmp_path, monkeypatch):
    from sqlalchemy import update

    from app.jobs import repository
    from app.jobs.runner import Worker
    from app.jobs.schema import collection_jobs, resolution_requests, source_runtime

    factory = fake_source(monkeypatch, tmp_path)
    repository.submit(conn, "https://www.bilibili.com/bangumi/play/ep1")
    original = repository.resolve_request

    def lose_before_commit(connection, *args, **kwargs):
        with db_engine.begin() as operator:
            operator.execute(update(source_runtime).values(worker_token=None))
        return original(connection, *args, **kwargs)

    monkeypatch.setattr(repository, "resolve_request", lose_before_commit)
    with Worker(db_engine, tmp_path / "jobs", factory) as worker:
        assert worker.run_once()["status"] == "interrupted"
    with conn.begin():
        assert conn.scalar(select(func.count()).select_from(collection_jobs)) == 0
        assert conn.scalar(select(resolution_requests.c.status)) == "resolving"


def test_full_queue_resolution_reuses_its_capacity_slot(conn, db_engine, tmp_path, monkeypatch):
    from app.jobs import repository
    from app.jobs.runner import Worker

    factory = fake_source(monkeypatch, tmp_path)
    monkeypatch.setattr(repository, "QUEUE_CAPACITY", 1)
    repository.submit(conn, "https://www.bilibili.com/bangumi/play/ep1")
    with Worker(db_engine, tmp_path / "jobs", factory) as worker:
        assert worker.run_once()["status"] == "ready"
        assert worker.run_once()["status"] == "succeeded"
