import httpx

from app.comment_export import collector
from app.comment_export.checkpoint import Checkpoint
from app.comment_export.source import CollectionStopped


def test_worker_guard_stops_page_commit_without_poisoning_source_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        collector,
        "resolve_video",
        lambda *_: {
            "source": {
                "platform": "bilibili",
                "aid": "1",
                "oid": "1",
                "bvid": "BVfake",
                "episode_id": None,
                "comment_type": 1,
            },
            "canonical_url": "https://www.bilibili.com/video/BVfake",
            "title": "sample",
        },
    )
    monkeypatch.setattr(collector, "get_signing_keys", lambda *_: ("a", "b"))
    monkeypatch.setattr(
        collector,
        "fetch_main",
        lambda *_: {
            "replies": [
                {
                    "rpid": 100,
                    "root": 0,
                    "parent": 0,
                    "member": {"mid": 1, "uname": "user"},
                    "content": {"message": "root"},
                    "ctime": 1,
                    "like": 0,
                    "rcount": 0,
                }
            ],
            "cursor": {"is_end": True, "all_count": 1},
        },
    )
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        client.collection_guard = lambda: (_ for _ in ()).throw(CollectionStopped("lease_lost"))
        rows, _ = collector.collect("url", tmp_path, client)
    assert rows == []
    cp = Checkpoint(tmp_path / "work.sqlite3")
    try:
        progress = cp.get_progress()
    finally:
        cp.close()
    assert progress["stopped_reason"] == "lease_lost"
    assert progress["blocked"] is False
