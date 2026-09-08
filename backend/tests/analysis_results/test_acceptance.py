from uuid import uuid4

import pytest
from result_examples import response_for

from app.analysis_packets.builder import json_bytes
from app.analysis_results.acceptance import accept_response, load_catalog


def send(case, packet, response, *, authorized=True, delivery=True):
    from dataclasses import replace

    from result_examples import execution_for

    execution = replace(
        execution_for(case, packet, response["attempt_id"]),
        authorized=authorized,
        delivery_verified=delivery,
    )
    return accept_response(
        case["root"],
        case["assembly"].run_id,
        case["publication"]["manifest_id"],
        packet["task_id"],
        json_bytes(response),
        execution=execution,
        rule_catalog=case["rules"],
    )


def test_accepted_roundtrip_and_repeat(output_case):
    case = output_case
    packet = case["assembly"].packets[0]
    response = response_for(packet, str(uuid4()))
    first = send(case, packet, response)
    assert first["decision"] == "accepted"
    assert send(case, packet, response) == first
    catalog = load_catalog(
        case["root"], case["assembly"].run_id, case["publication"]["manifest_id"], case["rules"]
    )
    assert catalog[packet["task_id"]].observations == ()


@pytest.mark.parametrize("flag", ["authorized", "delivery"])
def test_untrusted_execution_cannot_accept(output_case, flag):
    case = output_case
    packet = case["assembly"].packets[0]
    result = send(case, packet, response_for(packet, str(uuid4())), **{flag: False})
    assert result["decision"] == "rejected"
    assert not load_catalog(
        case["root"], case["assembly"].run_id, case["publication"]["manifest_id"], case["rules"]
    )


def test_incomplete_result_not_in_catalog(output_case):
    case = output_case
    packet = case["assembly"].packets[0]
    response = response_for(packet, str(uuid4()))
    response["model_status"] = "unable"
    response["coverage"]["processed_comment_ids"] = []
    response["coverage"]["unprocessed_targets"] = [
        {"comment_id": cid, "reason": "unable_to_process", "detail": "合成失败"}
        for cid in packet["scope"]["target_comment_ids"]
    ]
    assert send(case, packet, response)["decision"] == "incomplete"
    assert not load_catalog(
        case["root"], case["assembly"].run_id, case["publication"]["manifest_id"], case["rules"]
    )


def test_different_attempt_does_not_replace(output_case):
    case = output_case
    packet = case["assembly"].packets[0]
    first = send(case, packet, response_for(packet, str(uuid4())))
    second = send(case, packet, response_for(packet, str(uuid4())))
    assert second["decision"] == "rejected" and second["reason_codes"] == ["already_accepted"]
    assert first["result_ref"] is not None


def test_disk_result_tampering_rejected(output_case):
    from pathlib import Path

    case = output_case
    packet = case["assembly"].packets[0]
    receipt = send(case, packet, response_for(packet, str(uuid4())))
    (Path(case["publication"]["run_path"]) / receipt["result_ref"]["path"]).write_bytes(b"{}")
    with pytest.raises(ValueError):
        load_catalog(
            case["root"], case["assembly"].run_id, case["publication"]["manifest_id"], case["rules"]
        )


def test_orphan_result_needs_receipt_and_can_resume(output_case):
    from pathlib import Path

    case = output_case
    packet = case["assembly"].packets[0]
    response = response_for(packet, str(uuid4()))
    orphan = (
        Path(case["publication"]["run_path"])
        / f"results/{packet['task_id']}/{response['attempt_id']}.json"
    )
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(json_bytes(response))
    assert not load_catalog(
        case["root"], case["assembly"].run_id, case["publication"]["manifest_id"], case["rules"]
    )
    assert send(case, packet, response)["decision"] == "accepted"


def test_receipt_failure_then_same_attempt_recovers(output_case, monkeypatch):
    from app.analysis_results import acceptance

    case = output_case
    packet = case["assembly"].packets[0]
    response = response_for(packet, str(uuid4()))
    original = acceptance._write_once

    def fail_receipt(path, data):
        if path.name == "accepted.json":
            raise OSError("simulated interruption")
        return original(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(acceptance, "_write_once", fail_receipt)
        with pytest.raises(OSError):
            send(case, packet, response)
    assert not load_catalog(
        case["root"], case["assembly"].run_id, case["publication"]["manifest_id"], case["rules"]
    )
    assert send(case, packet, response)["decision"] == "accepted"


def test_offline_null_schema_cannot_be_accepted(output_case):
    from app.analysis_packets.builder import build_primary
    from app.analysis_packets.publication import publish_assembly

    case = dict(output_case)
    original = case["assembly"]
    unbound = build_primary(
        case["bundle"],
        str(uuid4()),
        original.resources,
        case["budget"],
        resource_files=original.resource_files,
    )
    from result_examples import register_members

    register_members(unbound)
    case["assembly"] = unbound
    case["publication"] = publish_assembly(case["root"], unbound, case["bundle"])
    packet = unbound.packets[0]
    result = send(case, packet, response_for(packet, str(uuid4())))
    assert result["decision"] == "rejected" and result["reason_codes"] == [
        "output_contract_missing"
    ]


def test_same_attempt_different_bytes_conflicts(output_case):
    case = output_case
    packet = case["assembly"].packets[0]
    response = response_for(packet, str(uuid4()))
    first = send(case, packet, response)
    response["summary"] = "Different summary"
    second = send(case, packet, response)
    assert first["decision"] == "accepted" and second["reason_codes"] == ["result_conflict"]


def test_catalog_rechecks_output_schema_on_disk(output_case):
    from pathlib import Path

    case = output_case
    packet = case["assembly"].packets[0]
    send(case, packet, response_for(packet, str(uuid4())))
    (
        Path(case["publication"]["run_path"]) / packet["response_contract"]["schema_ref"]["path"]
    ).unlink()
    with pytest.raises(ValueError):
        load_catalog(
            case["root"], case["assembly"].run_id, case["publication"]["manifest_id"], case["rules"]
        )


def test_execution_configuration_cannot_be_reassigned(output_case):
    from pathlib import Path

    from app.analysis_packets.codec import loads

    case = output_case
    packet = case["assembly"].packets[0]
    send(case, packet, response_for(packet, str(uuid4())))
    path = Path(case["publication"]["run_path"]) / "execution.json"
    value = loads(path.read_text(encoding="utf-8"))
    value["configured_model"] = "another-model"
    path.write_bytes(json_bytes(value))
    with pytest.raises(ValueError):
        load_catalog(
            case["root"], case["assembly"].run_id, case["publication"]["manifest_id"], case["rules"]
        )


def test_unregistered_agent_cannot_accept(output_case):
    from dataclasses import replace

    from result_examples import execution_for

    case = output_case
    packet = case["assembly"].packets[0]
    response = response_for(packet, str(uuid4()))
    execution = replace(
        execution_for(case, packet, response["attempt_id"]), agent_id="unregistered"
    )
    receipt = accept_response(
        case["root"],
        case["assembly"].run_id,
        case["publication"]["manifest_id"],
        packet["task_id"],
        json_bytes(response),
        execution=execution,
        rule_catalog=case["rules"],
    )
    assert receipt["decision"] == "rejected"
    assert not load_catalog(
        case["root"], case["assembly"].run_id, case["publication"]["manifest_id"], case["rules"]
    )


def test_catalog_requires_zero_observation_ancestor_receipt(output_case):
    from pathlib import Path

    from test_profiles import accept_all, full_pipeline

    case = output_case
    full_pipeline(case)
    accept_all(case)
    root_task = next(
        p
        for p in case["assembly"].packets
        if p["task_type"] == "thread_context" and p["phase"] == "primary"
    )
    (
        Path(case["publication"]["run_path"]) / f"validation/{root_task['task_id']}/accepted.json"
    ).unlink()
    with pytest.raises(ValueError):
        load_catalog(
            case["root"], case["assembly"].run_id, case["publication"]["manifest_id"], case["rules"]
        )
