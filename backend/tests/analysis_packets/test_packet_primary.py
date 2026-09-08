from uuid import uuid4

from app.analysis_packets.budget import Budget
from app.analysis_packets.builder import build_primary, prepare_resources
from app.analysis_packets.source import load_source


def test_primary_covers_each_target_once(prepared_case, resource_config):
    root, record = prepared_case()
    source = load_source(root, record["analysis_run_id"], record["video_id"])
    resources, files = prepare_resources(source, resource_config)
    assembly = build_primary(
        source, str(uuid4()), resources, Budget(100000, 1000, 110000, 2), resource_files=files
    )
    initial = [p for p in assembly.packets if p["task_type"] == "user_initial"]
    assert {p["scope"]["target_uid"] for p in initial} == {"10", "20"}
    assert all(p["prior_observations"] == [] for p in assembly.packets)
    for group in assembly.group_coverage["groups"]:
        expected = {
            row["comment_id"] for row in assembly.target_indexes[group["target_index"]["path"]]
        }
        seen = [cid for item in group["published_tasks"] for cid in item["target_comment_ids"]]
        pending = [item["comment_id"] for item in group["pending_targets"]]
        assert len(seen) == len(set(seen))
        assert set(seen) | set(pending) == expected


def test_missing_role_resource_is_not_silently_ignored(prepared_case, resource_config):
    import pytest

    root, record = prepared_case()
    source = load_source(root, record["analysis_run_id"], record["video_id"])
    resource_config["role_prompt"]["name"] = "missing.md"
    with pytest.raises(ValueError, match="invalid_resources"):
        prepare_resources(source, resource_config)


def test_comment_content_extension_keeps_number_precision(prepared_case):
    from decimal import Decimal

    from app.analysis_packets.builder import json_bytes

    encoded = json_bytes(
        {"content": {"text": "original", "extension": Decimal("0.123456789012345678901")}}
    )
    assert b"0.123456789012345678901" in encoded
    assert b'"0.123456789012345678901"' not in encoded


def test_oversize_target_has_no_fake_packet(prepared_case, resource_config):
    root, record = prepared_case(overlong=True)
    source = load_source(root, record["analysis_run_id"], record["video_id"])
    resources, files = prepare_resources(source, resource_config)
    assembly = build_primary(
        source, str(uuid4()), resources, Budget(10000, 1000, 12000, 2), resource_files=files
    )
    group = next(
        g
        for g in assembly.group_coverage["groups"]
        if g["task_type"] == "user_initial" and g["target_uid"] == "10"
    )
    assert [r["comment_id"] for r in group["pending_targets"]] == ["200"]
    assert len(group["published_tasks"]) == 1
    packet = next(
        p for p in assembly.packets if p["task_id"] == group["published_tasks"][0]["task_id"]
    )
    assert packet["chunk"]["count"] == 1
    assert packet["scope"]["target_comment_ids"] == ["100"]
    empty = next(
        g
        for g in assembly.group_coverage["groups"]
        if g["task_type"] == "thread_context" and g["root_ids"] == ["200"]
    )
    assert not empty["published_tasks"] and empty["pending_targets"]


def test_resources_alone_over_budget(prepared_case, resource_config):
    import pytest

    root, record = prepared_case()
    source = load_source(root, record["analysis_run_id"], record["video_id"])
    resources, files = prepare_resources(source, resource_config)
    with pytest.raises(ValueError, match="input_budget_exceeded"):
        build_primary(source, str(uuid4()), resources, Budget(1, 100, 200, 2), resource_files=files)
