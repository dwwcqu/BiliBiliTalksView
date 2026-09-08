"""Synthetic frozen delivery tests; no model calls."""

from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from app.analysis_execution.delivery import prepare_delivery
from app.analysis_packets.builder import sha
from app.analysis_packets.codec import json_bytes, loads
from app.analysis_results.errors import OutputError


def deliver(case, **kwargs):
    assembly = case["assembly"]
    packet = assembly.packets[0]
    return prepare_delivery(
        case["root"], assembly.run_id, case["publication"]["manifest_id"],
        packet["task_id"], str(uuid4()),
        kwargs.pop("agent_id", assembly.member_registry["members"][0]["agent_id"]),
        kwargs.pop("rules", case["rules"]), **kwargs,
    )


def test_delivery_preserves_frozen_input_and_schema(output_case):
    delivery = deliver(output_case)
    wrapper = loads(delivery.message)
    payload = wrapper["payload"]
    assert wrapper["payload_sha256"] == sha(json_bytes(payload)) == delivery.delivery_sha256
    assert payload["packet"] == output_case["assembly"].packets[0]
    assert payload["input_sha256"] == sha(json_bytes(delivery.packet))
    assert payload["resources"] == {
        path: raw.decode("utf-8")
        for path, raw in output_case["assembly"].resource_files.items()
    }
    assert payload["output_schema"]["$id"] in payload["schemas"]
    assert delivery.message in delivery.prompt
    assert not (delivery.run_path / "deliveries").exists()
    with pytest.raises(FrozenInstanceError):
        delivery.session_id = "changed"


def test_delivery_rejects_unknown_member(output_case):
    with pytest.raises(OutputError, match="unregistered_execution_agent"):
        deliver(output_case, agent_id="missing")


def test_delivery_rejects_rules_mismatch(output_case):
    with pytest.raises(OutputError, match="rules_hash_mismatch"):
        deliver(output_case, rules=output_case["rules"] | {"rules_sha256": "0" * 64})


def test_delivery_rejects_frozen_resource_tampering(output_case):
    delivery = deliver(output_case)
    resource = delivery.packet["resources"]["analysis_rules"]["path"]
    (delivery.run_path / resource).write_text("changed", encoding="utf-8")
    with pytest.raises((OutputError, ValueError)):
        deliver(output_case)


def test_delivery_full_prompt_budget(output_case, monkeypatch):
    from app.analysis_execution import delivery as module

    actual = module.read_state

    def small(*args):
        state = actual(*args)
        state[2].packets[0]["chunk"]["input_token_limit"] = 1
        return state

    monkeypatch.setattr(module, "read_state", small)
    with pytest.raises(OutputError, match="input_budget_exceeded"):
        deliver(output_case)

@pytest.mark.parametrize("field,value", [
    ("status", "failed"), ("role", "user_synthesis"), ("task_ids", []),
    ("role_sha256", "0" * 64), ("parent_session_id", str(uuid4())),
])
def test_delivery_checks_member_binding(output_case, monkeypatch, field, value):
    from app.analysis_execution import delivery as module

    actual = module.read_state

    def changed(*args):
        state = actual(*args)
        state[2].member_registry["members"][0][field] = value
        return state

    monkeypatch.setattr(module, "read_state", changed)
    with pytest.raises(OutputError, match="unregistered_execution_agent"):
        deliver(output_case)


def test_delivery_schema_closure_excludes_unreachable_documents(output_case):
    from app.analysis_results.contract import schema_document

    payload = loads(deliver(output_case).message)["payload"]
    assert schema_document("validation_receipt")["$id"] not in payload["schemas"]
    assert schema_document("user_profile")["$id"] not in payload["schemas"]
    for document in payload["schemas"].values():
        nodes = [document]
        while nodes:
            node = nodes.pop()
            if isinstance(node, list):
                nodes.extend(node)
            elif isinstance(node, dict):
                if "$ref" in node:
                    target = node["$ref"].split("#", 1)[0] or document["$id"]
                    assert target in payload["schemas"]
                nodes.extend(node.values())


def test_delivery_counts_prompt_instruction_overhead(output_case, monkeypatch):
    from app.analysis_execution import delivery as module

    initial = deliver(output_case)
    limit = len(initial.message.encode("utf-8")) + 40
    assert limit < len(initial.prompt.encode("utf-8"))
    actual = module.read_state

    def bounded(*args):
        state = actual(*args)
        state[2].packets[0]["chunk"]["input_token_limit"] = limit
        return state

    monkeypatch.setattr(module, "read_state", bounded)
    with pytest.raises(OutputError, match="input_budget_exceeded"):
        deliver(output_case)


def test_delivery_rejects_noncanonical_schema(output_case, monkeypatch):
    from app.analysis_execution import delivery as module

    actual = module.read_state

    def changed(*args):
        state = actual(*args)
        packet = state[2].packets[0]
        ref = packet["response_contract"]["schema_ref"]
        raw = json_bytes({"type": "object"})
        state[2].output_schemas[ref["path"]] = raw
        ref["sha256"] = sha(raw)
        return state

    monkeypatch.setattr(module, "read_state", changed)
    with pytest.raises(OutputError, match="output_schema_mismatch"):
        deliver(output_case)


def test_delivery_rejects_missing_label_catalog(output_case):
    with pytest.raises(OutputError, match="rule_catalog_missing"):
        deliver(output_case, rules={"rules_sha256": output_case["rules"]["rules_sha256"]})


@pytest.mark.parametrize("labels", [None, {}, {"topic_stance": [" "]}])
def test_delivery_rejects_invalid_label_catalog(output_case, labels):
    with pytest.raises(OutputError, match="rule_catalog_missing"):
        deliver(output_case, rules=output_case["rules"] | {"labels": labels})


def test_delivery_preserves_label_catalog_and_accepts_empty_labels(output_case):
    rules = output_case["rules"] | {
        "labels": {dimension: [] for dimension in output_case["rules"]["labels"]}
    }
    assert loads(deliver(output_case, rules=rules).message)["payload"]["label_catalog"] == rules


@pytest.mark.parametrize("value", ["label", [None], [" "], [1]])
def test_delivery_rejects_invalid_dimension_labels(output_case, value):
    labels = output_case["rules"]["labels"] | {"topic_stance": value}
    with pytest.raises(OutputError, match="rule_catalog_missing"):
        deliver(output_case, rules=output_case["rules"] | {"labels": labels})
