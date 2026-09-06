from fastapi.testclient import TestClient

from app.main import create_app

TOKEN = "test-admin-token-never-publish"
SECRET = "stable-test-rate-limit-secret"


def api(engine, **kwargs):
    return TestClient(
        create_app(database_engine=engine, admin_token=TOKEN, rate_limit_secret=SECRET, **kwargs)
    )


def test_http_submit_creates_request_and_reuses_it(db_engine, monkeypatch):
    from app.comment_export import source

    monkeypatch.setattr(
        source, "resolve_video", lambda *_: (_ for _ in ()).throw(AssertionError("network"))
    )
    with api(db_engine) as client:
        first = client.post(
            "/api/v1/video-requests", json={"url": "https://www.bilibili.com/bangumi/play/ep1/"}
        )
        assert first.status_code == 202
        again = client.post(
            "/api/v1/video-requests",
            json={"url": "https://www.bilibili.com/bangumi/play/ep1/?share_source=copy"},
        )
        assert again.status_code == 200
        assert first.json()["request_id"] == again.json()["request_id"]
        result = client.get("/api/v1/video-requests/" + first.json()["request_id"])
        assert result.json()["status"] == "queued"
        assert "owner_token" not in result.json()


def test_admin_requires_header_token_before_database_access(tmp_path):
    with TestClient(create_app(tmp_path, admin_token=TOKEN, rate_limit_secret=SECRET)) as client:
        result = client.post("/api/v1/admin/source/revalidate?token=" + TOKEN)
        assert result.status_code == 401
        assert TOKEN not in result.text


def test_body_limit_and_extra_fields_do_not_enqueue(db_engine):
    with api(db_engine) as client:
        assert client.post("/api/v1/video-requests", content=b"x" * 8193).status_code == 413
        assert (
            client.post(
                "/api/v1/video-requests",
                json={"url": "https://www.bilibili.com/bangumi/play/ep1", "cookie": "secret"},
            ).status_code
            == 422
        )


def store_case(conn, frozen_case, tmp_path, publish=True):
    from app.comment_export.export import build_batch
    from app.storage.frozen import freeze_batch
    from app.storage.importer import import_batch

    with freeze_batch(build_batch(*frozen_case, tmp_path / "batch"), tmp_path / "freeze") as batch:
        return import_batch(conn, batch, publish=publish)


def test_discussion_routes_preserve_identity_unicode_and_cursor_scope(
    conn, db_engine, frozen_case, tmp_path
):
    rows, _ = frozen_case
    rows[0]["content"]["text"] = "original\0escaped\ud800"
    stored = store_case(conn, frozen_case, tmp_path)
    prefix = "/api/v1/states/" + stored["state_id"]
    with api(db_engine) as client:
        first = client.get(prefix + "/threads?limit=1")
        assert first.status_code == 200
        assert first.json()["items"][0]["root_id"] == "100"
        second = client.get(
            prefix + "/threads", params={"limit": 1, "cursor": first.json()["next_cursor"]}
        )
        assert second.json()["items"][0]["root_id"] == "200"
        root = client.get(prefix + "/threads/100/comments?limit=1").json()
        assert root["items"][0]["kind"] == "root"
        reply = client.get(
            prefix + "/threads/100/comments", params={"cursor": root["next_cursor"]}
        ).json()
        assert reply["items"][0]["author"]["uid"] is None
        unknown = client.get(prefix + "/users/unknown/comments").json()
        assert unknown["items"][0]["content"]["text"] == "original\0escaped\ud800"
        assert {
            r["comment_id"] for r in client.get(prefix + "/users/1/comments").json()["items"]
        } == {"100", "200"}
        assert (
            client.get(
                prefix + "/threads/200/comments", params={"cursor": root["next_cursor"]}
            ).status_code
            == 422
        )
        assert client.get(prefix + "/threads?limit=201").status_code == 422
        assert client.get(prefix + "/threads/999/comments").status_code == 404
        summary = client.get("/api/v1/videos/bilibili:video:10001").json()
        assert summary["state"]["counts"]["comments"] == 3
        assert "refresh_context" not in summary


def test_unpublished_and_reclaimed_states_are_not_public(conn, db_engine, frozen_case, tmp_path):
    from uuid import uuid4

    from app.comment_export.export import build_batch
    from app.storage.frozen import freeze_batch
    from app.storage.importer import import_batch
    from app.storage.publication import publish_state

    stored = store_case(conn, frozen_case, tmp_path, publish=False)
    old_path = "/api/v1/states/" + stored["state_id"] + "/threads"
    with api(db_engine) as client:
        assert client.get(old_path).status_code == 410
        publish_state(conn, frozen_case[1]["video_id"], stored["state_id"])
        assert client.get(old_path).status_code == 200
        rows, meta = frozen_case
        meta["export_id"] = str(uuid4())
        for row in rows:
            row["export_id"] = meta["export_id"]
        with freeze_batch(
            build_batch(rows, meta, tmp_path / "new"), tmp_path / "new-freeze"
        ) as batch:
            import_batch(conn, batch, publish=True)
        assert client.get(old_path).status_code == 410


def test_new_intent_limits_survive_new_app_instance_and_rollback_queue(db_engine, monkeypatch):
    from datetime import UTC, datetime

    from sqlalchemy import func, select

    from app.api import rate_limits
    from app.jobs.schema import resolution_requests

    monkeypatch.setattr(rate_limits, "_utc_now", lambda _: datetime(2026, 9, 6, 8, 10, tzinfo=UTC))
    with api(db_engine) as client:
        for number in (1, 2, 3):
            assert (
                client.post(
                    "/api/v1/video-requests",
                    json={"url": f"https://www.bilibili.com/bangumi/play/ep{number}"},
                ).status_code
                == 202
            )
        assert (
            client.post(
                "/api/v1/video-requests", json={"url": "https://www.bilibili.com/bangumi/play/ep1"}
            ).status_code
            == 200
        )
    with api(db_engine) as client:
        rejected = client.post(
            "/api/v1/video-requests", json={"url": "https://www.bilibili.com/bangumi/play/ep4"}
        )
        assert rejected.status_code == 429
        assert rejected.json()["limit"] == "intent"
        assert int(rejected.headers["retry-after"]) > 0
    with db_engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(resolution_requests)) == 3


def test_write_limit_also_applies_to_reused_requests(db_engine, monkeypatch):
    from datetime import UTC, datetime

    from app.api import rate_limits

    monkeypatch.setattr(rate_limits, "_utc_now", lambda _: datetime(2026, 9, 6, 8, 10, tzinfo=UTC))
    with api(db_engine) as client:
        for _ in range(30):
            assert client.post(
                "/api/v1/video-requests", json={"url": "https://www.bilibili.com/bangumi/play/ep1"}
            ).status_code in {200, 202}
        response = client.post(
            "/api/v1/video-requests", json={"url": "https://www.bilibili.com/bangumi/play/ep1"}
        )
        assert response.status_code == 429 and response.json()["limit"] == "write"


def test_authorized_admin_only_changes_control_intent(conn, db_engine):
    from sqlalchemy import insert

    from app.jobs import repository
    from app.storage.schema import video_links, videos

    with conn.begin():
        conn.execute(
            insert(videos).values(
                video_id="bilibili:video:1", platform="bilibili", aid="1", oid="1", comment_type=1
            )
        )
        conn.execute(
            insert(video_links).values(
                video_id="bilibili:video:1",
                normalized_url="https://www.bilibili.com/video/BV1234567890",
            )
        )
    job = repository.request_refresh(conn, "bilibili:video:1")
    with api(db_engine) as client:
        path = "/api/v1/admin/jobs/" + job["job_id"] + "/cancel"
        assert client.post(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert repository.get_job(conn, job["job_id"])["status"] == "queued"
        result = client.post(path, headers={"Authorization": "Bearer " + TOKEN})
        assert result.status_code == 202 and result.json()["status"] == "cancelled"
        assert "owner_token" not in result.json()
        assert (
            client.post(
                path.replace("cancel", "retry"), headers={"Authorization": "Bearer " + TOKEN}
            ).status_code
            == 409
        )


def test_missing_configuration_and_database_errors_are_safe(tmp_path):
    from sqlalchemy.exc import OperationalError

    class BrokenEngine:
        def connect(self):
            raise OperationalError("private SQL", {}, RuntimeError("password-never-echo"))

    with TestClient(create_app(tmp_path, database_engine=BrokenEngine())) as client:
        result = client.get("/api/v1/jobs/00000000-0000-4000-8000-000000000000")
        assert result.status_code == 503
        assert "password-never-echo" not in result.text
    with TestClient(create_app(tmp_path, admin_token="")) as client:
        assert client.post("/api/v1/admin/source/revalidate").status_code == 503


def test_chunked_oversized_body_is_rejected(db_engine):
    with api(db_engine) as client:
        result = client.post(
            "/api/v1/video-requests",
            content=iter([b"x" * 4096, b"y" * 4097]),
            headers={"Content-Type": "application/json"},
        )
        assert result.status_code == 413


def test_concurrent_duplicate_intents_charge_once(db_engine):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import select

    from app.api.schema import api_rate_limits

    with api(db_engine) as client, ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: client.post(
                    "/api/v1/video-requests",
                    json={"url": "https://www.bilibili.com/bangumi/play/ep1"},
                ),
                range(4),
            )
        )
    assert sorted(result.status_code for result in results) == [200, 200, 200, 202]
    with db_engine.connect() as conn:
        assert (
            conn.scalar(select(api_rate_limits.c.hits).where(api_rate_limits.c.kind == "intent"))
            == 1
        )


def test_rate_limit_and_job_share_acceptance_time(db_engine, monkeypatch):
    from app.api import routes
    from app.jobs import repository

    stamps = []
    original_charge, original_submit = routes.charge, repository.submit

    def charge(conn, key, kind, **kwargs):
        stamps.append(kwargs.get("now"))
        return original_charge(conn, key, kind, **kwargs)

    def submit(conn, url, **kwargs):
        stamps.append(kwargs.get("now"))
        return original_submit(conn, url, **kwargs)

    monkeypatch.setattr(routes, "charge", charge)
    monkeypatch.setattr(repository, "submit", submit)
    with api(db_engine) as client:
        assert (
            client.post(
                "/api/v1/video-requests", json={"url": "https://www.bilibili.com/bangumi/play/ep1"}
            ).status_code
            == 202
        )
    assert stamps[0] is not None
    assert stamps == [stamps[0]] * 3


def test_manual_refresh_creates_once_then_reuses_job(
    conn, db_engine, frozen_case, tmp_path, monkeypatch
):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.api import routes

    fixed = datetime(2026, 9, 6, 8, 10, tzinfo=UTC)
    monkeypatch.setattr(routes, "datetime", SimpleNamespace(now=lambda _: fixed))
    store_case(conn, frozen_case, tmp_path)
    with api(db_engine) as client:
        first = client.post("/api/v1/videos/bilibili:video:10001/refresh", json={"mode": "auto"})
        assert first.status_code == 202
        repeat = client.post("/api/v1/videos/bilibili:video:10001/refresh", json={"mode": "full"})
        assert repeat.status_code == 200
        assert repeat.json()["job_id"] == first.json()["job_id"]
        job = client.get("/api/v1/jobs/" + first.json()["job_id"])
        assert job.json()["requested_mode"] == "auto"
        assert "baseline_version" not in job.json()
        assert client.post("/api/v1/videos/bilibili:video:999/refresh", json={}).status_code == 404


def test_imported_fresh_cache_blocks_refresh_without_spending_new_intent(
    conn, db_engine, frozen_case, tmp_path, monkeypatch
):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from sqlalchemy import select

    from app.api import routes
    from app.api.schema import api_rate_limits

    fixed = datetime(2026, 9, 6, 8, 10, tzinfo=UTC)
    monkeypatch.setattr(routes, "datetime", SimpleNamespace(now=lambda _: fixed))
    rows, meta = frozen_case
    url = "https://www.bilibili.com/video/BV1234567890"
    meta.update(
        input_url=url,
        canonical_url=url,
        hour_bucket="2026-09-06T16:00:00+08:00",
        captured_to="2026-09-06T08:10:00Z",
        exported_at="2026-09-06T08:11:00Z",
    )
    meta["source"]["bvid"] = "BV1234567890"
    for row in rows:
        row["collected_at"] = "2026-09-06T08:10:00Z"
    store_case(conn, frozen_case, tmp_path)
    with api(db_engine) as client:
        for _ in range(4):
            assert client.post("/api/v1/video-requests", json={"url": url}).status_code == 200
        blocked = client.post("/api/v1/videos/bilibili:video:10001/refresh", json={})
        assert blocked.status_code == 409
        assert blocked.json()["refresh_block_reason"] == "fresh_cache"
    with conn.begin():
        assert (
            conn.scalar(select(api_rate_limits.c.hits).where(api_rate_limits.c.kind == "intent"))
            is None
        )
