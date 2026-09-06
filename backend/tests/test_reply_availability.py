import httpx

from app.comment_export import collector
from app.comment_export.access_control import AccessControl
from app.comment_export.diagnostics import FailureDetail, classify_failure
from app.comment_export.source import CollectionStopped


def test_unavailable_reply_is_not_global_cooldown(tmp_path):
    endpoint = "/x/v2/reply/reply"
    assert classify_failure(200, 12022, endpoint) == "resource_unavailable"
    assert classify_failure(200, 12006, endpoint) == "resource_unavailable"
    control = AccessControl(tmp_path)
    control.record_failure(FailureDetail(endpoint=endpoint, http_status=200, api_code=12022,
        phase="replies", category="resource_unavailable", target={"root_id": "100", "page": 1}))
    assert control.read_status()["cooldown_until"] is None
    assert classify_failure(429, 12022, endpoint) == "rate_limited"


def test_unavailable_floor_preserved_and_other_floors_continue(monkeypatch, tmp_path):
    def root(cid):
        return {"rpid": cid, "root": 0, "member": {"mid": 1, "uname": "用户"},
                "content": {"message": "原评论"}, "ctime": 1, "like": 0, "rcount": 0}
    monkeypatch.setattr(collector, "resolve_video", lambda *args: {
        "source": {"platform": "bilibili", "aid": "1", "oid": "1", "bvid": "BV1234567890",
                   "episode_id": None, "comment_type": 1},
        "canonical_url": "https://www.bilibili.com/video/BV1234567890", "title": "test"})
    monkeypatch.setattr(collector, "get_signing_keys", lambda client: ("a", "b"))
    monkeypatch.setattr(collector, "fetch_main", lambda *args: {
        "replies": [root(100), root(200)], "cursor": {"is_end": True, "all_count": 2}})
    called = []
    def replies(source, rid, page, client):
        called.append(rid)
        if rid == "100":
            raise CollectionStopped("source_reply_unavailable", FailureDetail(
                endpoint="/x/v2/reply/reply", phase="replies", http_status=200, api_code=12022,
                category="resource_unavailable", video_id="bilibili:video:1",
                target={"root_id": rid, "page": page}))
        return {"root": root(200), "page": {"num": 1, "size": 20, "count": 0}, "replies": []}
    monkeypatch.setattr(collector, "fetch_replies", replies)
    with httpx.Client() as client:
        records, meta = collector.collect("https://www.bilibili.com/video/BV1234567890", tmp_path, client, 100)
    assert called == ["100", "200"]
    assert {r["comment_id"] for r in records} == {"100", "200"}
    assert meta["_threads"]["100"]["pagination_status"] == "partial"
    assert meta["_threads"]["100"]["unavailable"]["api_code"] == 12022
    assert meta["_threads"]["200"]["pagination_status"] == "verified"
    assert meta["coverage"]["status"] == "partial"
