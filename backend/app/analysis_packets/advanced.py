"""Pure later-stage construction with explicitly trusted frozen result catalogs.

The returned copy includes all prior effective groups for an immutable complete manifest.
"""

from copy import deepcopy

from .builder import _measure, add_group, json_bytes, sha
from .errors import PacketError
from .validation import _accepted, validate_assembly


def _observations(results, uid=None):
    observations = []
    for result in results:
        for original in result.observations:
            if uid is not None and original["uid"] != uid:
                continue
            item = deepcopy(original)
            item["origin_result"] = {"path": result.result_path, "sha256": sha(result.result_bytes)}
            observations.append(item)
    return observations


def _refresh(assembly):
    loaded = b"".join(
        assembly.resource_files[assembly.resources[key]["path"]]
        for key in ("analysis_rules", "role_prompt", "coordination")
    )
    packets = {packet["task_id"]: packet for packet in assembly.packets}
    for row in assembly.task_rows:
        packet = packets[row["task_id"]]
        _measure(packet, loaded)
        row["input_sha256"] = sha(json_bytes(packet))


def build_reconcile(bundle, primary_assembly, accepted, budget):
    """Append reconcile groups; missing primary outputs leave targets explicitly pending."""
    validate_assembly(primary_assembly, bundle, accepted)
    assembly = deepcopy(primary_assembly)
    groups = [
        g
        for g in primary_assembly.group_coverage["groups"]
        if g["task_type"] == "thread_context" and g["phase"] == "primary"
    ]
    known = {p["task_id"]: p for p in primary_assembly.packets}
    users = [
        result for task, result in accepted.items() if result.packet["task_type"] == "user_initial"
    ]
    for result in users:
        _accepted(result.packet["task_id"], accepted, assembly, bundle)
    for group in groups:
        primary_packets = [known[item["task_id"]] for item in group["published_tasks"]]
        completed = [
            _accepted(p["task_id"], accepted, assembly, bundle)
            for p in primary_packets
            if p["task_id"] in accepted
        ]
        covered = {
            cid for result in completed for cid in result.packet["scope"]["target_comment_ids"]
        }
        targets = {r["comment_id"] for r in assembly.target_indexes[group["target_index"]["path"]]}
        add_group(
            assembly,
            bundle,
            task_type="thread_context",
            phase="reconcile",
            target_uid=None,
            root_ids=group["root_ids"],
            synthesis_level=None,
            budget=budget,
            observations=_observations([*completed, *users]),
            dependency_packets=[r.packet for r in completed],
            pending_ids=targets - covered,
        )
    validate_assembly(assembly, bundle, accepted)
    return assembly


def build_synthesis(bundle, target_uid, synthesis_level, prior_assembly, accepted, budget):
    """Append one UID's next layer, retaining its complete source target index."""
    if target_uid not in bundle.users or type(synthesis_level) is not int or synthesis_level < 0:
        raise PacketError("invalid_synthesis_scope")
    validate_assembly(prior_assembly, bundle, accepted)
    assembly = deepcopy(prior_assembly)
    selected = []
    for task, result in accepted.items():
        packet = result.packet
        if synthesis_level == 0:
            eligible = (
                packet["task_type"] == "user_initial"
                and packet["scope"]["target_uid"] == target_uid
            ) or (packet["task_type"] == "thread_context" and packet["phase"] == "reconcile")
        else:
            eligible = (
                packet["task_type"] == "user_synthesis"
                and packet["scope"]["target_uid"] == target_uid
                and packet["synthesis_level"] == synthesis_level - 1
            )
        if eligible:
            selected.append(_accepted(task, accepted, assembly, bundle))
    targets = set(bundle.users[target_uid])
    covered = {cid for r in selected for cid in r.packet["scope"]["target_comment_ids"]}

    def adjust_coverage(packet):
        if synthesis_level > 0:
            packet["coverage"]["summary_used"] = True
            for result in selected:
                if not set(packet["scope"]["target_comment_ids"]) & set(
                    result.packet["scope"]["target_comment_ids"]
                ):
                    continue
                for gap in result.packet["coverage"]["context_gaps"]:
                    if (
                        gap["kind"] == "prior_result_missing"
                        and gap not in packet["coverage"]["context_gaps"]
                    ):
                        packet["coverage"]["context_gaps"].append(deepcopy(gap))
                for limitation in result.packet["coverage"]["limitations"]:
                    if limitation not in packet["coverage"]["limitations"]:
                        packet["coverage"]["limitations"].append(limitation)
        else:
            reconciled = {
                cid
                for result in selected
                if result.packet["task_type"] == "thread_context"
                for cid in result.packet["scope"]["target_comment_ids"]
            }
            missing = set(packet["scope"]["target_comment_ids"]) - reconciled
            if missing:
                packet["coverage"]["context_gaps"].append(
                    {
                        "kind": "prior_result_missing",
                        "comment_ids": sorted(missing, key=int),
                        "root_ids": sorted(
                            {bundle.comments_by_id[cid]["root_id"] for cid in missing}, key=int
                        ),
                        "detail": "Related accepted reconcile results are not available.",
                    }
                )
                packet["coverage"]["limitations"].append("Only partial synthesis is supported.")

    add_group(
        assembly,
        bundle,
        task_type="user_synthesis",
        phase="primary",
        target_uid=target_uid,
        root_ids=(),
        synthesis_level=synthesis_level,
        budget=budget,
        observations=_observations(selected, target_uid),
        dependency_packets=[r.packet for r in selected],
        pending_ids=targets - covered,
        coverage_adjuster=adjust_coverage,
    )
    _refresh(assembly)
    validate_assembly(assembly, bundle, accepted)
    return assembly
