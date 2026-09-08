"""Synthetic, disk-backed profile publication tests."""

import json
from uuid import uuid4

import pytest
from result_examples import execution_for

from app.analysis_packets.builder import DIMENSIONS, json_bytes, sha
from app.analysis_results.errors import OutputError


def execution_ref(case):
    from app.analysis_results.acceptance import load_context

    run, _, _, _ = load_context(
        case["root"], case["assembly"].run_id, case["publication"]["manifest_id"]
    )
    from result_examples import ensure_execution

    return run, ensure_execution(case)


def save(case, uid, ref):
    from app.analysis_results.profiles import save_profile

    return save_profile(
        case["root"],
        case["assembly"].run_id,
        case["publication"]["manifest_id"],
        uid,
        case["rules"],
        ref,
    )


def test_unaccepted_tasks_only_publish_candidate(output_case):
    case = output_case
    run, ref = execution_ref(case)
    uid = next(iter(case["bundle"].users))
    saved = save(case, uid, ref)
    profile = json.loads((run / saved["path"]).read_bytes())
    assert saved["artifact_type"] == "profile_candidate"
    assert profile["analysis_coverage"]["status"] == "partial"
    assert profile["analysis_coverage"]["missing_task_ids"]
    assert profile["source_results"] == []
    assert [d["dimension"] for d in profile["dimensions"]] == DIMENSIONS
    assert all(d["assessment_status"] == "insufficient" for d in profile["dimensions"])
    assert profile["source_coverage"] == case["bundle"].manifest["coverage"]
    assert profile["corpus_counts"] == case["bundle"].manifest["counts"]
    assert not (run / "users" / (uid + ".json")).exists()
    assert save(case, uid, ref)["path"] != saved["path"]


def test_unknown_uid_and_untrusted_execution_are_rejected(output_case):
    case = output_case
    run, ref = execution_ref(case)
    with pytest.raises(OutputError, match="unknown_uid"):
        save(case, "99999999999", ref)
    uid = next(iter(case["bundle"].users))
    with pytest.raises(OutputError, match="invalid_execution"):
        save(case, uid, ref | {"sha256": "0" * 64})
    with pytest.raises(OutputError, match="invalid_execution"):
        save(case, uid, ref | {"path": "../execution.json"})
    (run / "execution.json").write_bytes(json_bytes({"configured_model": "fake"}))
    with pytest.raises(OutputError, match="invalid_execution"):
        save(case, uid, ref | {"sha256": sha((run / "execution.json").read_bytes())})


def accept_all(case, *, not_applicable=False):
    from result_examples import execution_for, response_for

    from app.analysis_results.acceptance import accept_response, load_catalog

    catalog = load_catalog(
        case["root"], case["assembly"].run_id, case["publication"]["manifest_id"], case["rules"]
    )
    for packet in case["assembly"].packets:
        if packet["task_id"] in catalog:
            continue
        attempt = str(uuid4())
        response = response_for(packet, attempt)
        if not_applicable:
            for dimension in response["dimension_results"]:
                dimension["assessment_status"] = "not_applicable"
        accept_response(
            case["root"],
            case["assembly"].run_id,
            case["publication"]["manifest_id"],
            packet["task_id"],
            json_bytes(response),
            execution=execution_for(case, packet, attempt),
            rule_catalog=case["rules"],
        )
    return load_catalog(
        case["root"], case["assembly"].run_id, case["publication"]["manifest_id"], case["rules"]
    )


def publish(case, assembly, catalog):
    from app.analysis_packets.publication import publish_assembly
    from app.analysis_results.acceptance import bind_output_schema

    assembly.previous_manifest_sha256 = case["publication"]["manifest_sha256"]
    from result_examples import register_members

    case["assembly"] = register_members(bind_output_schema(assembly))
    case["publication"] = publish_assembly(
        case["root"], case["assembly"], case["bundle"], accepted=catalog
    )


def full_pipeline(case):
    from app.analysis_packets.advanced import build_reconcile, build_synthesis

    catalog = accept_all(case)
    publish(
        case, build_reconcile(case["bundle"], case["assembly"], catalog, case["budget"]), catalog
    )
    catalog = accept_all(case)
    uid = next(iter(case["bundle"].users))
    publish(
        case,
        build_synthesis(case["bundle"], uid, 0, case["assembly"], catalog, case["budget"]),
        catalog,
    )
    return uid


def test_complete_pipeline_is_final_and_retry_preserves_file(output_case):
    case = output_case
    uid = full_pipeline(case)
    run, ref = execution_ref(case)
    assert save(case, uid, ref)["artifact_type"] == "profile_candidate"
    accept_all(case)
    saved = save(case, uid, ref)
    assert saved["path"] == f"users/{uid}.json"
    before = (run / saved["path"]).read_bytes()
    profile = json.loads(before)
    assert profile["analysis_coverage"]["status"] == "complete"
    assert profile["analysis_coverage"]["missing_task_ids"] == []
    assert profile["summary"] == "本批输入未作出确定判断。"
    assert profile["source_results"]
    assert save(case, uid, ref) == saved
    assert (run / saved["path"]).read_bytes() == before
    profile["summary"] = "changed"
    (run / saved["path"]).write_bytes(json_bytes(profile))
    with pytest.raises(OutputError, match="profile_conflict"):
        save(case, uid, ref)


def test_new_unaccepted_highest_layer_blocks_final(output_case):
    from app.analysis_packets.advanced import build_synthesis

    case = output_case
    uid = full_pipeline(case)
    catalog = accept_all(case)
    publish(
        case,
        build_synthesis(case["bundle"], uid, 1, case["assembly"], catalog, case["budget"]),
        catalog,
    )
    _, ref = execution_ref(case)
    assert save(case, uid, ref)["artifact_type"] == "profile_candidate"


def test_initial_only_synthesis_cannot_hide_missing_reconcile(output_case):
    from app.analysis_packets.advanced import build_synthesis

    case = output_case
    catalog = accept_all(case)
    uid = next(iter(case["bundle"].users))
    publish(
        case,
        build_synthesis(case["bundle"], uid, 0, case["assembly"], catalog, case["budget"]),
        catalog,
    )
    accept_all(case)
    run, ref = execution_ref(case)
    saved = save(case, uid, ref)
    profile = json.loads((run / saved["path"]).read_bytes())
    assert profile["analysis_coverage"]["missing_task_ids"] == []
    assert profile["analysis_coverage"]["unprocessed_targets"] == []
    assert profile["artifact_type"] == "profile_candidate"
    assert not (run / f"users/{uid}.json").exists()


def test_accepted_result_tampering_prevents_publication(output_case):
    case = output_case
    uid = full_pipeline(case)
    catalog = accept_all(case)
    run, ref = execution_ref(case)
    result = next(iter(catalog.values()))
    (run / result.result_path).write_bytes(result.result_bytes + b" ")
    with pytest.raises(OutputError):
        save(case, uid, ref)
    assert not (run / f"users/{uid}.json").exists()


def test_final_observations_reference_frozen_result_bytes(output_case):
    from result_examples import response_for

    from app.analysis_results.acceptance import accept_response, load_catalog

    case = output_case
    uid = full_pipeline(case)
    packet = case["assembly"].packets[-1]
    attempt = str(uuid4())
    response = response_for(packet, attempt)
    cid = packet["scope"]["target_comment_ids"][0]
    observation = {
        "observation_id": packet["task_id"] + ":o1",
        "origin_task_id": packet["task_id"],
        "origin_type": "user_synthesis",
        "uid": uid,
        "dimension": "topic_stance",
        "subject": {
            "kind": "unknown",
            "label": None,
            "proposition": None,
            "target_uid": None,
            "evidence_comment_ids": [],
        },
        "labels": [],
        "assessment_status": "insufficient",
        "rationale": "合成样例证据不足。",
        "source_comment_ids": [cid],
        "evidence": [],
        "counter_evidence": [],
        "limitations": ["证据不足。"],
        "export_id": packet["export_id"],
        "rules_sha256": packet["resources"]["analysis_rules"]["sha256"],
    }
    response["observations"] = [observation]
    response["dimension_results"][0]["observation_ids"] = [observation["observation_id"]]
    accept_response(
        case["root"],
        case["assembly"].run_id,
        case["publication"]["manifest_id"],
        packet["task_id"],
        json_bytes(response),
        execution=execution_for(case, packet, attempt),
        rule_catalog=case["rules"],
    )
    run, ref = execution_ref(case)
    saved = save(case, uid, ref)
    profile = json.loads((run / saved["path"]).read_bytes())
    catalog = load_catalog(
        case["root"], case["assembly"].run_id, case["publication"]["manifest_id"], case["rules"]
    )
    origin = catalog[packet["task_id"]]
    assert profile["dimensions"][0]["observations"] == [
        observation
        | {"origin_result": {"path": origin.result_path, "sha256": sha(origin.result_bytes)}}
    ]
    assert profile["dimensions"][0]["assessment_status"] == "insufficient"


def test_candidate_directions_without_observations_remain_insufficient(output_case):
    case = output_case
    accept_all(case, not_applicable=True)
    run, ref = execution_ref(case)
    uid = next(iter(case["bundle"].users))
    saved = save(case, uid, ref)
    profile = json.loads((run / saved["path"]).read_bytes())
    assert profile["artifact_type"] == "profile_candidate"
    assert all(d["assessment_status"] == "insufficient" for d in profile["dimensions"])


def test_verified_source_with_context_gaps_keeps_limitation(frozen_case, request):
    frozen_case[1]["coverage"]["context_status"] = "gaps"
    case = request.getfixturevalue("output_case")
    run, ref = execution_ref(case)
    uid = next(iter(case["bundle"].users))
    saved = save(case, uid, ref)
    profile = json.loads((run / saved["path"]).read_bytes())
    assert profile["source_coverage"]["status"] == "verified"
    assert profile["source_coverage"]["context_status"] == "gaps"
    assert any("源采集存在缺口" in item for item in profile["limitations"])


def test_coverage_traverses_dependencies_from_previous_manifest(output_case):
    from copy import deepcopy

    from app.analysis_results.profiles import _coverage

    case = output_case
    uid = full_pipeline(case)
    catalog = accept_all(case)
    assembly = deepcopy(case["assembly"])
    historical = next(
        row
        for row in assembly.task_rows
        if row["task_type"] == "user_initial" and row["target_uid"] == uid
    )
    assembly.task_rows = [r for r in assembly.task_rows if r["task_id"] != historical["task_id"]]
    assembly.packets = [p for p in assembly.packets if p["task_id"] != historical["task_id"]]
    coverage, final, _ = _coverage(
        case["bundle"], assembly, catalog, uid, {historical["task_id"]: historical}
    )
    assert coverage["status"] == "complete"
    assert historical["task_id"] in coverage["required_task_ids"]
    assert historical["task_id"] in coverage["accepted_task_ids"]
    assert final is not None
