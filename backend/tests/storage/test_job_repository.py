"""Persistent collection intent repository behavior against isolated PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, update

from app.jobs.errors import JobError
from app.jobs.repository import (
    control_job,
    get_job,
    hour_bucket,
    normalize_url,
    request_refresh,
    resolve_request,
    submit,
    video_view,
)
from app.jobs.schema import collection_jobs, resolution_requests, source_runtime
from app.storage.schema import video_links, videos

NOW = datetime(2026, 9, 6, 8, 59, 30, tzinfo=UTC)  # 16:59 Beijing


def _video(connection, aid="42", url="https://www.bilibili.com/video/BV1234567890"):
    video_id = f"bilibili:video:{aid}"
    connection.execute(
        insert(videos).values(
            video_id=video_id, platform="bilibili", aid=aid, oid=aid, comment_type=1
        )
    )
    connection.execute(insert(video_links).values(normalized_url=url, video_id=video_id))
    connection.commit()
    return video_id


@pytest.mark.parametrize(
    "url,want",
    [
        (
            "https://www.bilibili.com/video/BV1234567890/?spm_id_from=333",
            "https://www.bilibili.com/video/BV1234567890",
        ),
        (
            "https://www.bilibili.com/bangumi/play/ep123/?from=search",
            "https://www.bilibili.com/bangumi/play/ep123",
        ),
    ],
)
def test_normalize_url_accepts_only_supported_canonical_paths(url, want):
    assert normalize_url(url) == want


@pytest.mark.parametrize(
    "url",
    [
        "http://www.bilibili.com/video/BV1234567890",
        "https://bilibili.com/video/BV1234567890",
        "https://u:p@www.bilibili.com/video/BV1234567890",
        "https://www.bilibili.com:443/video/BV1234567890",
        "https://www.bilibili.com/video/BV1234567890#x",
        "https://www.bilibili.com/bangumi/play/ep0",
    ],
)
def test_normalize_url_rejects_unsafe_or_unsupported_urls(url):
    with pytest.raises(JobError, match="invalid_url"):
        normalize_url(url)


def test_hour_bucket_uses_beijing_natural_hour():
    assert hour_bucket(NOW) == datetime(2026, 9, 6, 8, 0, tzinfo=UTC)


def test_unknown_submit_reuses_resolution_in_original_hour(db_engine):
    url = "https://www.bilibili.com/video/BV1234567890?share_source=copy_web"
    with db_engine.connect() as conn:
        first = submit(conn, url, now=NOW)
        second = submit(conn, url, now=NOW + timedelta(seconds=20))
        row = conn.execute(select(resolution_requests)).mappings().one()
    assert first == second
    assert first["disposition"] == "request"
    assert row["accepted_at"] == NOW
    assert row["hour_bucket"] == datetime(2026, 9, 6, 8, 0, tzinfo=UTC)


def test_known_submit_creates_one_first_job_and_failed_is_not_implicit_retry(db_engine):
    with db_engine.connect() as conn:
        video_id = _video(conn)
        first = submit(conn, "https://www.bilibili.com/video/BV1234567890", now=NOW)
        conn.execute(update(collection_jobs).values(status="failed", safe_error="source_failed"))
        conn.commit()
        repeated = submit(conn, "https://www.bilibili.com/video/BV1234567890", now=NOW)
    assert first["disposition"] == "job"
    assert repeated["job_id"] == first["job_id"]
    assert repeated["video_id"] == video_id


def test_concurrent_refresh_has_one_active_job(db_engine):
    with db_engine.connect() as conn:
        video_id = _video(conn)

    def refresh():
        with db_engine.connect() as worker:
            return request_refresh(worker, video_id, now=NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: refresh(), range(2)))
    assert len({result["job_id"] for result in results}) == 1


def test_refresh_reuses_cross_hour_active_job_then_respects_fresh_import(db_engine, state_factory):
    with db_engine.connect() as conn:
        video_id = _video(conn)
        old = request_refresh(conn, video_id, now=NOW)
        reused = request_refresh(conn, video_id, now=NOW + timedelta(hours=2))
        conn.execute(update(collection_jobs).values(status="succeeded", completed_at=NOW))
        state = state_factory(
            conn,
            video_id=video_id,
            lifecycle="current",
            hour_bucket=datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
        )
        conn.execute(
            update(videos)
            .where(videos.c.video_id == video_id)
            .values(current_state_id=state["state_id"])
        )
        conn.commit()
        cached = request_refresh(conn, video_id, now=NOW + timedelta(hours=3, minutes=10))
    assert reused["job_id"] == old["job_id"]
    assert cached["disposition"] == "cache"
    assert cached["refresh_block_reason"] == "fresh_cache"


def test_video_view_prefers_current_and_reports_partial_separately(db_engine, state_factory):
    with db_engine.connect() as conn:
        video_id = _video(conn)
        current = state_factory(conn, video_id=video_id, lifecycle="current")
        partial = state_factory(
            conn,
            video_id=video_id,
            lifecycle="partial",
            captured_to=NOW + timedelta(minutes=1),
            exported_at=NOW + timedelta(minutes=1),
        )
        conn.execute(
            update(videos)
            .where(videos.c.video_id == video_id)
            .values(current_state_id=current["state_id"], working_state_id=partial["state_id"])
        )
        conn.commit()
        view = video_view(conn, video_id, now=NOW)
    assert view["state_id"] == current["state_id"]
    assert view["partial_state_id"] == partial["state_id"]


def test_resolve_request_keeps_acceptance_hour_and_registers_aliases(db_engine):
    with db_engine.connect() as conn:
        pending = submit(conn, "https://www.bilibili.com/bangumi/play/ep123", now=NOW)
        token = str(uuid4())
        conn.execute(
            update(resolution_requests).values(
                status="resolving", owner_token=token, lease_until=NOW + timedelta(hours=3)
            )
        )
        conn.execute(
            update(source_runtime).values(
                source_gate="recovering",
                blocked_kind="resolution",
                blocked_id=pending["request_id"],
                action="revalidate",
            )
        )
        conn.commit()
        result = resolve_request(
            conn,
            pending["request_id"],
            token,
            {
                "source": {
                    "platform": "bilibili",
                    "aid": "42",
                    "oid": "42",
                    "bvid": "BV1234567890",
                    "episode_id": "123",
                    "comment_type": 1,
                },
                "canonical_url": "https://www.bilibili.com/video/BV1234567890",
                "title": "title",
            },
            now=NOW + timedelta(hours=2),
            commit_guard=lambda connection: None,
        )
        job = get_job(conn, result["job_id"])
        aliases = set(conn.execute(select(video_links.c.normalized_url)).scalars())
        runtime = conn.execute(select(source_runtime)).mappings().one()
    assert job["hour_bucket"] == datetime(2026, 9, 6, 8, 0, tzinfo=UTC)
    assert aliases == {
        "https://www.bilibili.com/video/BV1234567890",
        "https://www.bilibili.com/bangumi/play/ep123",
    }
    assert runtime["source_gate"] == "normal"
    assert runtime["blocked_kind"] is None and runtime["blocked_id"] is None
    assert runtime["action"] is None


def test_job_controls_preserve_identity_and_budget_and_expired_result(db_engine, state_factory):
    with db_engine.connect() as conn:
        video_id = _video(conn)
        created = request_refresh(conn, video_id, now=NOW)
        conn.execute(
            update(collection_jobs)
            .where(collection_jobs.c.job_id == created["job_id"])
            .values(status="failed", requests=7, retry_count=1, result_state_id=None)
        )
        conn.commit()
        retried = control_job(conn, created["job_id"], "retry")
        conn.execute(
            update(collection_jobs)
            .where(collection_jobs.c.job_id == created["job_id"])
            .values(status="partial", result_state_id=None)
        )
        conn.commit()
        expired = get_job(conn, created["job_id"])
    assert retried["job_id"] == created["job_id"]
    assert retried["requests"] == 7 and retried["retry_count"] == 1
    assert expired["result_expired"] is True
