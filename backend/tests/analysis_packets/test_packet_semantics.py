from uuid import uuid4

import pytest

from app.analysis_packets.budget import Budget
from app.analysis_packets.builder import build_primary, json_bytes, prepare_resources, sha
from app.analysis_packets.errors import PacketError
from app.analysis_packets.source import load_source


def make_primary(prepared_case, resource_config):
    root, record = prepared_case()
    bundle = load_source(root, record["analysis_run_id"], record["video_id"])
    resources, files = prepare_resources(bundle, resource_config)
    budget = Budget(100000, 1000, 110000, 2)
    return (
        bundle,
        build_primary(bundle, str(uuid4()), resources, budget, resource_files=files),
        budget,
    )


def refresh(assembly):
    packets = {p["task_id"]: p for p in assembly.packets}
    for row in assembly.task_rows:
        row["input_sha256"] = sha(json_bytes(packets[row["task_id"]]))


def test_valid_primary_semantics(prepared_case, resource_config):
    from app.analysis_packets.validation import validate_assembly

    bundle, assembly, _ = make_primary(prepared_case, resource_config)
    validate_assembly(assembly, bundle, {})


@pytest.mark.parametrize(
    "mutation",
    ["text", "author", "coverage", "root", "partition", "task_index", "cycle", "resource"],
)
def test_rejects_semantic_corruption(prepared_case, resource_config, mutation):
    from app.analysis_packets.validation import validate_assembly

    bundle, assembly, _ = make_primary(prepared_case, resource_config)
    packet = assembly.packets[0]
    if mutation == "text":
        packet["comments"][0]["content"]["text"] = "rewritten"
    elif mutation == "author":
        packet["comments"][0]["author_uid"] = "999"
    elif mutation == "coverage":
        packet["coverage"]["source"]["reasons"] = []
        packet["coverage"]["corpus_counts"]["comments"] = 900
    elif mutation == "root":
        packet["scope"]["root_ids"] = ["999"]
    elif mutation == "partition":
        assembly.group_coverage["groups"][0]["pending_targets"].append(
            {
                "comment_id": packet["scope"]["target_comment_ids"][0],
                "reason": "dependency_missing",
                "detail": "missing",
            }
        )
    elif mutation == "task_index":
        assembly.task_rows[0]["target_uid"] = "999"
    elif mutation == "cycle":
        assembly.task_rows[0]["depends_on"] = [packet["task_id"]]
    else:
        assembly.resource_files["context/README.md"] = b"changed"
    refresh(assembly)
    with pytest.raises(PacketError):
        validate_assembly(assembly, bundle, {})


def test_missing_context_gap_is_rejected(prepared_case, resource_config):
    from app.analysis_packets.builder import _measure
    from app.analysis_packets.validation import validate_assembly

    bundle, assembly, _ = make_primary(prepared_case, resource_config)
    packet = next(p for p in assembly.packets if p["coverage"]["context_gaps"])
    packet["coverage"]["context_gaps"] = []
    loaded = b"".join(
        assembly.resource_files[assembly.resources[k]["path"]]
        for k in ("analysis_rules", "role_prompt", "coordination")
    )
    _measure(packet, loaded)
    refresh(assembly)
    with pytest.raises(PacketError, match="missing_context_gap"):
        validate_assembly(assembly, bundle, {})


@pytest.mark.parametrize("mutation", ["extra_file", "swapped_frozen_file"])
def test_resources_cannot_publish_extra_or_misbound_files(prepared_case, resource_config, mutation):
    from app.analysis_packets.validation import validate_assembly

    bundle, assembly, _ = make_primary(prepared_case, resource_config)
    if mutation == "extra_file":
        assembly.resource_files["users/10.json"] = b"{}"
    else:
        reference = assembly.resources["analysis_rules"]
        data = bundle.context_bytes["README.md"]
        assembly.resource_files[reference["path"]] = data
        reference["sha256"] = sha(data)
        from app.analysis_packets.builder import _measure

        loaded = b"".join(
            assembly.resource_files[assembly.resources[k]["path"]]
            for k in ("analysis_rules", "role_prompt", "coordination")
        )
        for packet in assembly.packets:
            packet["resources"]["analysis_rules"]["sha256"] = sha(data)
            _measure(packet, loaded)
        refresh(assembly)
    with pytest.raises(PacketError, match="resource"):
        validate_assembly(assembly, bundle, {})
