"""Paired positive/negative contract examples, not model-quality claims.

Every negative starts from a passing assembly and changes one semantic condition.
Packet size estimates and transport hashes are refreshed so semantic guards,
rather than incidental hash failures, must reject the input.
"""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from test_packet_advanced import accept, observation
from test_packet_semantics import make_primary

from app.analysis_packets.advanced import build_reconcile, build_synthesis
from app.analysis_packets.builder import _measure, json_bytes, jsonl_bytes, sha
from app.analysis_packets.errors import PacketError
from app.analysis_packets.publication import publish_assembly, validate_published
from app.analysis_packets.validation import validate_assembly


def repack(assembly):
    loaded = b"".join(
        assembly.resource_files[assembly.resources[k]["path"]]
        for k in ("analysis_rules", "role_prompt", "coordination")
    )
    packets = {p["task_id"]: p for p in assembly.packets}
    for row in assembly.task_rows:
        packet = packets[row["task_id"]]
        _measure(packet, loaded)
        row["input_sha256"] = sha(json_bytes(packet))


def with_observation(prepared_case, resource_config, *, insufficient=False):
    bundle, primary, budget = make_primary(prepared_case, resource_config)
    accepted = {p["task_id"]: accept(p) for p in primary.packets}
    origin = next(
        p
        for p in primary.packets
        if p["task_type"] == "user_initial" and p["scope"]["target_uid"] == "10"
    )
    item = observation(origin, bundle)
    if insufficient:
        item.update(
            assessment_status="insufficient",
            labels=[],
            evidence=[],
            subject={
                "kind": "unknown",
                "label": None,
                "proposition": None,
                "target_uid": None,
                "evidence_comment_ids": [],
            },
        )
    else:
        item["labels"] = ["支持"]  # Injected fixture observation, not a model prediction.
    accepted[origin["task_id"]] = accept(origin, [item])
    return bundle, primary, budget, accepted, origin, item


@pytest.mark.parametrize(
    "case,expected",
    [
        ("changed_original", "source_projection_mismatch"),
        ("wrong_author", "source_projection_mismatch"),
        ("hidden_coverage", "source_coverage_mismatch"),
        ("missing_context_gap", "missing_context_gap"),
        ("wrong_index_uid", "task_index_identity_mismatch"),
    ],
)
def test_primary_positive_then_single_semantic_negative(
    prepared_case, resource_config, case, expected
):
    bundle, positive, _ = make_primary(prepared_case, resource_config)
    validate_assembly(positive, bundle, {})
    negative = deepcopy(positive)
    packet = (
        next(p for p in negative.packets if p["task_type"] == "user_initial")
        if case == "wrong_index_uid"
        else next(p for p in negative.packets if p["coverage"]["context_gaps"])
    )
    target = next(r for r in packet["comments"] if r["input_role"] == "target")
    if case == "changed_original":
        target["content"]["text"] = "原文不存在的改写"
    elif case == "wrong_author":
        target["author_uid"] = "999"
    elif case == "hidden_coverage":
        packet["coverage"]["source"]["reasons"] = []
        packet["coverage"]["source"]["context_status"] = "no_known_gaps"
    elif case == "missing_context_gap":
        packet["coverage"]["context_gaps"] = []
    else:
        next(r for r in negative.task_rows if r["task_id"] == packet["task_id"])["target_uid"] = (
            "999"
        )
    repack(negative)
    with pytest.raises(PacketError, match=expected):
        validate_assembly(negative, bundle, {})
    validate_assembly(positive, bundle, {})


def test_complete_uid_index_then_consistent_but_incomplete_index(prepared_case, resource_config):
    bundle, positive, _ = make_primary(prepared_case, resource_config)
    validate_assembly(positive, bundle, {})
    negative = deepcopy(positive)
    group = next(
        g
        for g in negative.group_coverage["groups"]
        if g["task_type"] == "user_initial" and g["target_uid"] == "10"
    )
    ref = group["target_index"]
    rows = negative.target_indexes[ref["path"]]
    assert len(rows) == 2
    rows.pop()
    ref.update(count=len(rows), sha256=sha(jsonl_bytes(rows)))
    for packet in negative.packets:
        if packet["chunk"]["group_id"] == group["group_id"]:
            packet["scope"]["target_index"] = deepcopy(ref)
    repack(negative)
    with pytest.raises(PacketError, match="incomplete_target_index"):
        validate_assembly(negative, bundle, {})


@pytest.mark.parametrize(
    "case,expected",
    [
        ("invented_quote", "invalid_evidence_quote"),
        ("unlocated_subject", "invalid_packet"),
        ("no_positive_evidence", "missing_observation_evidence"),
    ],
)
def test_valid_observation_then_bad_semantics_in_accepted_result(
    prepared_case, resource_config, case, expected
):
    bundle, primary, budget, accepted, origin, item = with_observation(
        prepared_case, resource_config
    )
    positive = build_reconcile(bundle, primary, accepted, budget)
    validate_assembly(positive, bundle, accepted)
    bad = deepcopy(item)
    if case == "invented_quote":
        bad["evidence"][0]["quote"] = "This quote is not in the comment."
    elif case == "unlocated_subject":
        bad["subject"] = {
            "kind": "unknown",
            "label": None,
            "proposition": None,
            "target_uid": None,
            "evidence_comment_ids": [],
        }
    else:
        bad["evidence"] = []
    negative_catalog = dict(accepted)
    negative_catalog[origin["task_id"]] = accept(origin, [bad])
    with pytest.raises(PacketError, match=expected):
        build_reconcile(bundle, primary, negative_catalog, budget)


def test_insufficient_observation_without_evidence_is_allowed(prepared_case, resource_config):
    bundle, primary, budget, accepted, _, _ = with_observation(
        prepared_case, resource_config, insufficient=True
    )
    assembly = build_reconcile(bundle, primary, accepted, budget)
    validate_assembly(assembly, bundle, accepted)
    values = [o for p in assembly.packets for o in p["prior_observations"]]
    assert values and all(o["assessment_status"] == "insufficient" for o in values)


def test_declared_observation_dependency_then_omitted_dependency(prepared_case, resource_config):
    bundle, primary, budget, accepted, origin, _ = with_observation(prepared_case, resource_config)
    positive = build_reconcile(bundle, primary, accepted, budget)
    validate_assembly(positive, bundle, accepted)
    negative = deepcopy(positive)
    packet = next(p for p in negative.packets if p["prior_observations"])
    row = next(r for r in negative.task_rows if r["task_id"] == packet["task_id"])
    row["depends_on"].remove(origin["task_id"])
    with pytest.raises(PacketError, match="undeclared_result_dependency"):
        validate_assembly(negative, bundle, accepted)


def test_completed_result_then_running_result(prepared_case, resource_config):
    bundle, primary, budget, accepted, origin, _ = with_observation(prepared_case, resource_config)
    validate_assembly(build_reconcile(bundle, primary, accepted, budget), bundle, accepted)
    incomplete = dict(accepted)
    incomplete[origin["task_id"]] = replace(accepted[origin["task_id"]], status="running")
    with pytest.raises(PacketError, match="unaccepted_result"):
        build_reconcile(bundle, primary, incomplete, budget)


def test_layered_synthesis_then_wrong_level_dependency(prepared_case, resource_config):
    bundle, primary, budget = make_primary(prepared_case, resource_config)
    accepted = {p["task_id"]: accept(p) for p in primary.packets}
    reconciled = build_reconcile(bundle, primary, accepted, budget)
    for packet in reconciled.packets:
        if packet["phase"] == "reconcile":
            accepted[packet["task_id"]] = accept(packet)
    level0 = build_synthesis(bundle, "10", 0, reconciled, accepted, budget)
    for packet in level0.packets:
        if packet["task_type"] == "user_synthesis":
            accepted[packet["task_id"]] = accept(packet)
    positive = build_synthesis(bundle, "10", 1, level0, accepted, budget)
    validate_assembly(positive, bundle, accepted)
    for group in positive.group_coverage["groups"]:
        if group["task_type"] == "user_synthesis":
            assert {
                r["comment_id"] for r in positive.target_indexes[group["target_index"]["path"]]
            } == set(bundle.users["10"])
    negative = deepcopy(positive)
    packet = next(p for p in negative.packets if p["synthesis_level"] == 1)
    row = next(r for r in negative.task_rows if r["task_id"] == packet["task_id"])
    old_initial = next(
        p
        for p in primary.packets
        if p["task_type"] == "user_initial" and p["scope"]["target_uid"] == "10"
    )
    row["depends_on"].append(old_initial["task_id"])
    with pytest.raises(PacketError, match="invalid_synthesis_dependency"):
        validate_assembly(negative, bundle, accepted)


def test_same_label_distinct_subjects_are_preserved(prepared_case, resource_config):
    bundle, primary, budget, accepted, origin, item = with_observation(
        prepared_case, resource_config
    )
    first, second = deepcopy(item), deepcopy(item)
    first["observation_id"] += "-music"
    first["subject"]["label"] = "配乐"
    second["observation_id"] += "-voice"
    second["subject"]["label"] = "配音"
    # This asserts preservation only; it does not assert model classification accuracy.
    accepted[origin["task_id"]] = accept(origin, [first, second])
    result = build_reconcile(bundle, primary, accepted, budget)
    subjects = {o["subject"]["label"] for p in result.packets for o in p["prior_observations"]}
    assert subjects == {"配乐", "配音"}


def test_two_manifest_publish_reload_then_disk_corruption(prepared_case, resource_config):
    bundle, primary, budget, accepted, _, _ = with_observation(prepared_case, resource_config)
    root, _ = prepared_case()
    first = publish_assembly(root, primary, bundle)
    advanced = build_reconcile(bundle, primary, accepted, budget)
    advanced.previous_manifest_sha256 = first["manifest_sha256"]
    second = publish_assembly(root, advanced, bundle, accepted)
    assert (
        validate_published(root, second["run_id"], second["manifest_id"], accepted)["status"]
        == "valid_offline"
    )
    assert Path(first["manifest_path"]).is_file()
    assert list((Path(second["run_path"]) / "users").iterdir()) == []
    origin = next(dep for row in advanced.task_rows for dep in row["depends_on"])
    (Path(second["run_path"]) / accepted[origin].result_path).write_bytes(b"{}")
    with pytest.raises(PacketError, match="reference_hash_mismatch"):
        validate_published(root, second["run_id"], second["manifest_id"], accepted)
