from copy import deepcopy

import pytest


@pytest.fixture
def frozen_case():
    base = {
        "schema_version": "1.0.0", "export_id": "94a08de2-a412-4b56-b0aa-7f606b854fc7",
        "video_id": "bilibili:video:10001", "comment_id": "100", "root_id": "100",
        "parent_id": None, "kind": "root", "author": {"uid": "1", "nickname": "甲/乙"},
        "reply_relation": {"status": "not_applicable", "target_uid": None},
        "content": {"text": "一\n二", "images": [], "emotes": []},
        "created_at": "2026-09-05T08:00:00Z", "collected_at": "2026-09-05T09:00:00Z",
        "like_count": 0,
    }
    second = deepcopy(base)
    second.update(comment_id="200", root_id="200")
    reply = deepcopy(base)
    reply.update(comment_id="101", parent_id="999", kind="reply")
    reply["reply_relation"]["status"] = "source"
    reply["author"] = {"uid": None, "nickname": None}
    metadata = {
        **{k: base[k] for k in ("schema_version", "export_id", "video_id")},
        "input_url": "https://www.bilibili.com/video/BVfake",
        "canonical_url": "https://www.bilibili.com/video/BVfake",
        "source": {"platform": "bilibili", "aid": "10001", "oid": "10001",
                   "bvid": "BVfake", "episode_id": None, "comment_type": 1},
        "title": "虚构视频", "captured_from": "2026-09-05T08:00:00Z",
        "captured_to": "2026-09-05T09:00:00Z", "exported_at": "2026-09-05T09:01:00Z",
        "hour_bucket": "2026-09-05T16:00:00+08:00", "source_reported_count": 3,
        "coverage": {"status": "verified", "main_pagination": "verified",
                     "replies_pagination": "verified", "context_status": "no_known_gaps",
                     "reasons": []},
        "_threads": {"100": {"pagination_status": "verified", "count": 1},
                     "200": {"pagination_status": "verified", "count": 0}},
    }
    return [reply, second, base], metadata
