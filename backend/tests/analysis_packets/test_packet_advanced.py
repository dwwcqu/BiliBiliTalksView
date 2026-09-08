from copy import deepcopy

import pytest
from test_packet_semantics import make_primary, refresh

from app.analysis_packets.builder import json_bytes
from app.analysis_packets.errors import PacketError
from app.analysis_packets.types import AcceptedResult


def accept(packet, observations=()):
    result = {
        key: packet[key]
        for key in ["task_id", "run_id", "video_id", "export_id", "synthesis_level"]
    }
    result.update(
        target_uid=packet["scope"]["target_uid"],
        rules_sha256=packet["resources"]["analysis_rules"]["sha256"],
        observations=list(observations),
    )
    return AcceptedResult(
        deepcopy(packet),
        f"results/{packet['task_id']}.json",
        json_bytes(result),
        tuple(deepcopy(observations)),
    )


def observation(packet, bundle, uid="10"):
    cid = next(
        cid
        for cid in packet["scope"]["target_comment_ids"]
        if bundle.comments_by_id[cid]["author"]["uid"] == uid
    )
    return {
        "observation_id": "observation-" + packet["task_id"],
        "origin_task_id": packet["task_id"],
        "origin_type": packet["task_type"],
        "origin_result": {"path": "pending", "sha256": "0" * 64},
        "uid": uid,
        "dimension": "topic_stance",
        "subject": {
            "kind": "topic",
            "label": "合成话题",
            "proposition": None,
            "target_uid": None,
            "evidence_comment_ids": [cid],
        },
        "labels": [],
        "assessment_status": "assessable",
        "rationale": "合成说明",
        "source_comment_ids": [cid],
        "evidence": [{"comment_id": cid, "quote": bundle.comments_by_id[cid]["content"]["text"]}],
        "counter_evidence": [],
        "limitations": [],
        "export_id": packet["export_id"],
        "rules_sha256": packet["resources"]["analysis_rules"]["sha256"],
    }


def test_reconcile_requires_accepted_primary_even_with_zero_observations(
    prepared_case, resource_config
):
    from app.analysis_packets.advanced import build_reconcile
    from app.analysis_packets.validation import validate_assembly

    bundle, primary, budget = make_primary(prepared_case, resource_config)
    accepted = {p["task_id"]: accept(p) for p in primary.packets}
    advanced = build_reconcile(bundle, primary, accepted, budget)
    validate_assembly(advanced, bundle, accepted)
    rows = {r["task_id"]: r for r in advanced.task_rows}
    packets = [p for p in advanced.packets if p["phase"] == "reconcile"]
    assert packets
    assert all(rows[p["task_id"]]["depends_on"] for p in packets)
    rows[packets[0]["task_id"]]["depends_on"] = []
    with pytest.raises(PacketError, match="missing_primary_dependency"):
        validate_assembly(advanced, bundle, accepted)
    assert all(p["phase"] == "primary" for p in primary.packets)


def test_reconcile_waits_for_missing_primary(prepared_case, resource_config):
    from app.analysis_packets.advanced import build_reconcile
    from app.analysis_packets.validation import validate_assembly

    bundle, primary, budget = make_primary(prepared_case, resource_config)
    advanced = build_reconcile(bundle, primary, {}, budget)
    groups = [g for g in advanced.group_coverage["groups"] if g["phase"] == "reconcile"]
    assert groups and all(not g["published_tasks"] and g["pending_targets"] for g in groups)
    validate_assembly(advanced, bundle, {})


def test_synthesis_levels_keep_full_target_index(prepared_case, resource_config):
    from app.analysis_packets.advanced import build_synthesis
    from app.analysis_packets.validation import validate_assembly

    bundle, primary, budget = make_primary(prepared_case, resource_config)
    accepted = {p["task_id"]: accept(p) for p in primary.packets}
    level0 = build_synthesis(bundle, "10", 0, primary, accepted, budget)
    for packet in level0.packets:
        if packet["task_type"] == "user_synthesis":
            accepted[packet["task_id"]] = accept(packet)
    level1 = build_synthesis(bundle, "10", 1, level0, accepted, budget)
    validate_assembly(level1, bundle, accepted)
    groups = [g for g in level1.group_coverage["groups"] if g["task_type"] == "user_synthesis"]
    assert len(groups) == 2
    assert all(g["target_index"]["count"] == len(bundle.users["10"]) for g in groups)
    packet = next(p for p in level1.packets if p["synthesis_level"] == 1)
    assert packet["coverage"]["summary_used"] is True


@pytest.mark.parametrize("mutation", ["dependency", "quote", "uid", "rules", "result"])
def test_observations_are_verified(prepared_case, resource_config, mutation):
    from app.analysis_packets.advanced import build_reconcile
    from app.analysis_packets.validation import validate_assembly

    bundle, primary, budget = make_primary(prepared_case, resource_config)
    source = next(
        p
        for p in primary.packets
        if p["task_type"] == "user_initial" and p["scope"]["target_uid"] == "10"
    )
    obs = observation(source, bundle)
    accepted = {p["task_id"]: accept(p) for p in primary.packets}
    accepted[source["task_id"]] = accept(source, [obs])
    advanced = build_reconcile(bundle, primary, accepted, budget)
    validate_assembly(advanced, bundle, accepted)
    packet = next(p for p in advanced.packets if p["prior_observations"])
    item = packet["prior_observations"][0]
    if mutation == "dependency":
        row = next(r for r in advanced.task_rows if r["task_id"] == packet["task_id"])
        row["depends_on"].remove(item["origin_task_id"])
    elif mutation == "quote":
        item["evidence"][0]["quote"] = "fabricated quotation"
    elif mutation == "uid":
        item["uid"] = "999"
    elif mutation == "rules":
        item["rules_sha256"] = "0" * 64
    else:
        item["origin_result"]["sha256"] = "0" * 64
    refresh(advanced)
    with pytest.raises(PacketError):
        validate_assembly(advanced, bundle, accepted)


@pytest.mark.parametrize("field", ["status", "run_id", "observations"])
def test_frozen_result_identity_and_status_are_checked(prepared_case, resource_config, field):
    from dataclasses import replace

    from app.analysis_packets.advanced import build_reconcile
    from app.comment_export.contract import parse_json

    bundle, primary, budget = make_primary(prepared_case, resource_config)
    accepted = {p["task_id"]: accept(p) for p in primary.packets}
    task = next(p["task_id"] for p in primary.packets if p["task_type"] == "thread_context")
    result = accepted[task]
    if field == "status":
        accepted[task] = replace(result, status="running")
    else:
        document = parse_json(result.result_bytes.decode("utf-8"))
        document[field] = "wrong" if field == "run_id" else [{"unaccepted": True}]
        accepted[task] = replace(result, result_bytes=json_bytes(document))
    with pytest.raises(PacketError):
        build_reconcile(bundle, primary, accepted, budget)


def test_missing_previous_layer_stays_pending(prepared_case, resource_config):
    from app.analysis_packets.advanced import build_synthesis
    from app.analysis_packets.validation import validate_assembly

    bundle, primary, budget = make_primary(prepared_case, resource_config)
    assembly = build_synthesis(bundle, "10", 1, primary, {}, budget)
    group = assembly.group_coverage["groups"][-1]
    assert not group["published_tasks"]
    assert {p["comment_id"] for p in group["pending_targets"]} == set(bundle.users["10"])
    assert group["final_merge_task_id"] is None
    validate_assembly(assembly, bundle, {})


def test_higher_synthesis_preserves_missing_prior_results(prepared_case, resource_config):
    from app.analysis_packets.advanced import build_synthesis

    bundle, primary, budget = make_primary(prepared_case, resource_config)
    accepted = {p["task_id"]: accept(p) for p in primary.packets}
    level0 = build_synthesis(bundle, "10", 0, primary, accepted, budget)
    for packet in level0.packets:
        if packet["task_type"] == "user_synthesis":
            accepted[packet["task_id"]] = accept(packet)
    level1 = build_synthesis(bundle, "10", 1, level0, accepted, budget)
    packet = next(p for p in level1.packets if p["synthesis_level"] == 1)
    assert any(g["kind"] == "prior_result_missing" for g in packet["coverage"]["context_gaps"])


def test_synthesis_accounts_for_missing_result_gaps_before_partition(
    prepared_case, resource_config
):
    from uuid import uuid4

    from app.analysis_packets.advanced import build_synthesis
    from app.analysis_packets.budget import Budget
    from app.analysis_packets.builder import build_primary
    from app.analysis_packets.validation import validate_assembly

    bundle, original, _ = make_primary(prepared_case, resource_config)
    budget = Budget(3050, 1000, 11000, 2)
    primary = build_primary(
        bundle, str(uuid4()), original.resources, budget, resource_files=original.resource_files
    )
    accepted = {packet["task_id"]: accept(packet) for packet in primary.packets}
    synthesis = build_synthesis(bundle, "10", 0, primary, accepted, budget)
    validate_assembly(synthesis, bundle, accepted)
    group = synthesis.group_coverage["groups"][-1]
    targets = {cid for item in group["published_tasks"] for cid in item["target_comment_ids"]}
    pending = {item["comment_id"] for item in group["pending_targets"]}
    assert targets | pending == set(bundle.users["10"])
    assert not targets & pending
    for packet in synthesis.packets:
        assert packet["chunk"]["estimated_input_tokens"] <= budget.max_input_tokens
