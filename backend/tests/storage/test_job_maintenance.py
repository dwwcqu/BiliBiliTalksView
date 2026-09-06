from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import insert, select, update

from app.jobs.maintenance import purge_metadata
from app.jobs.repository import request_refresh, submit
from app.jobs.schema import collection_jobs, resolution_requests
from app.storage.schema import video_links, videos


def failed_job(conn):
    stamp = datetime.now(UTC)
    with conn.begin():
        conn.execute(
            insert(videos).values(
                video_id="bilibili:video:1",
                platform="bilibili",
                aid="1",
                oid="1",
                bvid="BV1234567890",
                comment_type=1,
            )
        )
        conn.execute(
            insert(video_links).values(
                video_id="bilibili:video:1",
                normalized_url="https://www.bilibili.com/video/BV1234567890",
            )
        )
    job = request_refresh(conn, "bilibili:video:1", now=stamp)
    with conn.begin():
        conn.execute(
            update(collection_jobs)
            .where(collection_jobs.c.job_id == job["job_id"])
            .values(status="failed", completed_at=stamp)
        )
    return job["job_id"], stamp


def test_purge_removes_workspace_body_and_preserves_first_request_marker(conn, db_engine, tmp_path):
    identity, stamp = failed_job(conn)
    work = tmp_path / identity / "collection"
    work.mkdir(parents=True)
    (work / "work.sqlite3").write_text("synthetic body", encoding="utf-8")
    result = purge_metadata(db_engine, tmp_path, now=stamp + timedelta(days=8))
    assert result["jobs"] == 1
    assert not (work / "work.sqlite3").exists()
    assert (
        submit(conn, "https://www.bilibili.com/video/BV1234567890")["disposition"]
        == "refresh_required"
    )


def test_recent_resolution_reference_keeps_old_job(conn, db_engine, tmp_path):
    identity, stamp = failed_job(conn)
    future = stamp + timedelta(days=8)
    with conn.begin():
        conn.execute(
            insert(resolution_requests).values(
                request_id=str(uuid4()),
                normalized_url="https://www.bilibili.com/bangumi/play/ep2",
                accepted_at=future,
                hour_bucket=future.replace(minute=0, second=0, microsecond=0),
                status="ready",
                job_id=identity,
                completed_at=future,
            )
        )
    assert purge_metadata(db_engine, tmp_path, now=future)["jobs"] == 0
    with conn.begin():
        assert conn.scalar(select(collection_jobs.c.job_id)) == identity
        assert conn.scalar(select(resolution_requests.c.job_id)) == identity
