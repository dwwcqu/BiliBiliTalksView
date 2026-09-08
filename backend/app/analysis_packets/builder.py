"""Pure, offline construction of primary and dependency-bearing task groups."""

import hashlib
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

from .budget import METHOD, estimate_input
from .codec import json_bytes
from .context import select_context
from .partition import partition_targets
from .source import SourceBundle
from .types import Assembly

PROTOCOL = "BiliBiliTalksView.AnalysisInput"
VERSION = "2.0.0"
DIMENSIONS = [
    "topic_stance",
    "discourse_function",
    "argument_support",
    "response_engagement",
    "expressed_emotion",
    "interpersonal_expression",
    "conflict_cooperation",
    "view_revision",
]
IDENTITY = [
    "task_id",
    "run_id",
    "video_id",
    "export_id",
    "target_uid",
    "synthesis_level",
    "rules_sha256",
]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def jsonl_bytes(rows) -> bytes:
    return b"".join(json_bytes(row) for row in rows)


def prepare_resources(bundle: SourceBundle, config: dict) -> tuple[dict, dict[str, bytes]]:
    try:
        kinds = ("analysis_rules", "role_prompt", "coordination")
        if set(config) != set(kinds):
            raise ValueError
        source = bundle.context_bytes
        used = {"readme.md"}
        files = {"context/README.md": source["README.md"]}
        resources = {
            "background": {
                "path": "context/README.md",
                "sha256": sha(source["README.md"]),
                "text": source["README.md"].decode("utf-8"),
            }
        }
        for kind in kinds:
            name, version = config[kind]["name"], config[kind]["version"]
            if (
                not isinstance(name, str)
                or name not in source
                or name.casefold() in used
                or not isinstance(version, str)
                or not version
            ):
                raise ValueError
            used.add(name.casefold())
            path = "context/" + name
            files[path] = source[name]
            resources[kind] = {"path": path, "sha256": sha(source[name]), "version": version}
        return resources, files
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid_resources") from exc


def new_assembly(bundle, run_id, resources, resource_files, budget):
    if str(UUID(run_id)) != run_id:
        raise ValueError("invalid_run_id")
    # Resource text cost is separate from in-packet background text.
    loaded = b"".join(
        resource_files[resources[key]["path"]]
        for key in ("analysis_rules", "role_prompt", "coordination")
    )
    if estimate_input(json_bytes(resources), loaded) > budget.max_input_tokens:
        raise ValueError("input_budget_exceeded")
    return Assembly(
        run_id,
        resources=deepcopy(resources),
        resource_files=dict(resource_files),
        group_coverage={
            "protocol": PROTOCOL,
            "schema_version": VERSION,
            "run_id": run_id,
            "export_id": bundle.export_id,
            "groups": [],
        },
        member_registry={
            "protocol": PROTOCOL,
            "schema_version": VERSION,
            "registry_id": str(uuid4()),
            "run_id": run_id,
            "video_id": bundle.video_id,
            "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "main": {"session_id": None, "status": "uncreated"},
            "members": [],
        },
        limits=budget.limits(),
        context_window=budget.context_window,
    )


def _measure(packet, loaded):
    for _ in range(8):
        estimate = estimate_input(json_bytes(packet), loaded)
        if packet["chunk"]["estimated_input_tokens"] == estimate:
            return
        packet["chunk"]["estimated_input_tokens"] = estimate
    raise ValueError("unstable_size_estimate")


def add_group(
    assembly,
    bundle,
    *,
    task_type,
    phase,
    target_uid,
    root_ids,
    synthesis_level,
    budget,
    observations=(),
    dependency_packets=(),
    pending_ids=(),
    coverage_adjuster=None,
):
    rows = bundle._data["comments"]
    targets = (
        list(bundle._data["users"].get(target_uid, ()))
        if target_uid is not None
        else [cid for root in root_ids for cid in bundle._data["threads"].get(root, ())]
    )
    targets = sorted(
        set(targets),
        key=lambda cid: (
            int(rows[cid]["root_id"]),
            rows[cid]["created_at"] is None,
            rows[cid]["created_at"] or "",
            int(cid),
        ),
    )
    if not set(pending_ids) <= set(targets):
        raise ValueError("invalid_pending_targets")
    group_id = str(uuid4())
    index_path = f"indexes/targets-{group_id}.jsonl"
    index_rows = [
        {
            "comment_id": cid,
            "root_id": rows[cid]["root_id"],
            "author_uid": rows[cid]["author"]["uid"],
        }
        for cid in targets
    ]
    index = {"path": index_path, "sha256": sha(jsonl_bytes(index_rows)), "count": len(index_rows)}
    loaded = b"".join(
        assembly.resource_files[assembly.resources[key]["path"]]
        for key in ("analysis_rules", "role_prompt", "coordination")
    )

    def packet_factory(ids):
        chosen = [deepcopy(o) for o in observations if set(o["source_comment_ids"]) & set(ids)]
        evidence_ids = {
            e["comment_id"] for o in chosen for e in [*o["evidence"], *o["counter_evidence"]]
        }
        evidence_ids.update(cid for o in chosen for cid in o["subject"]["evidence_comment_ids"])
        selection = tuple(dict.fromkeys([*ids, *sorted(evidence_ids, key=int)]))
        comments, gaps = select_context(bundle, selection)
        for comment in comments:
            if comment["comment_id"] not in ids:
                comment["input_role"] = "context"
                comment["context_reasons"] = sorted(
                    (set(comment["context_reasons"]) - {"target"}) | {"cross_chunk_evidence"}
                    if comment["comment_id"] in evidence_ids
                    else set(comment["context_reasons"]) - {"target"}
                )
        packet = {
            "protocol": PROTOCOL,
            "schema_version": VERSION,
            "task_id": str(uuid4()),
            "run_id": assembly.run_id,
            "prepared_run_id": bundle.prepared_run_id,
            "video_id": bundle.video_id,
            "export_id": bundle.export_id,
            "export_schema_version": bundle.export_schema_version,
            "task_type": task_type,
            "phase": phase,
            "synthesis_level": synthesis_level,
            "scope": {
                "target_uid": target_uid,
                "root_ids": sorted({rows[cid]["root_id"] for cid in ids}, key=int),
                "target_comment_ids": list(ids),
                "context_comment_ids": [],
                "target_index": deepcopy(index),
            },
            "resources": deepcopy(assembly.resources),
            "comments": comments,
            "prior_observations": chosen,
            "coverage": {
                "source": deepcopy(bundle._data["manifest"]["coverage"]),
                "corpus_counts": deepcopy(bundle._data["manifest"]["counts"]),
                "context_gaps": gaps,
                "omitted_context_ids": [],
                "summary_used": bool(chosen),
                "limitations": [],
            },
            "chunk": {
                "group_id": group_id,
                "index": max(0, len(targets) - 1),
                "count": max(1, len(targets)),
                "estimator": METHOD,
                "estimated_input_tokens": 0,
                "input_token_limit": budget.max_input_tokens,
                "reserved_output_tokens": budget.reserved_output_tokens,
            },
            "response_contract": {
                "schema_ref": None,
                "required_dimensions": DIMENSIONS[:],
                "required_identity_fields": IDENTITY[:],
                "evidence_required": True,
                "allow_unknown": True,
            },
        }
        packet["scope"]["context_comment_ids"] = [
            r["comment_id"] for r in comments if r["input_role"] == "context"
        ]
        if coverage_adjuster is not None:
            coverage_adjuster(packet)
        _measure(packet, loaded)
        if packet["chunk"]["estimated_input_tokens"] > budget.max_input_tokens:
            removable = [
                r
                for r in comments
                if r["input_role"] == "context" and r["context_reasons"] == ["direct_reply"]
            ]
            if removable:
                removed = {r["comment_id"] for r in removable}
                packet["comments"] = [r for r in comments if r["comment_id"] not in removed]
                packet["scope"]["context_comment_ids"] = [
                    cid for cid in packet["scope"]["context_comment_ids"] if cid not in removed
                ]
                packet["coverage"]["omitted_context_ids"] = sorted(removed, key=int)
                gaps.append(
                    {
                        "kind": "context_budget",
                        "comment_ids": sorted(removed, key=int),
                        "root_ids": packet["scope"]["root_ids"],
                        "detail": "Optional direct replies omitted to fit offline capacity.",
                    }
                )
                _measure(packet, loaded)
        return packet

    packets, pending = partition_targets(
        targets, packet_factory, budget.max_input_tokens, pending_ids
    )
    published = []
    for index_number, packet in enumerate(packets):
        packet["chunk"].update(index=index_number, count=len(packets))
        _measure(packet, loaded)
        if packet["chunk"]["estimated_input_tokens"] > budget.max_input_tokens:
            raise ValueError("input_budget_exceeded")
        ids = set(packet["scope"]["target_comment_ids"])
        dependencies = {o["origin_task_id"] for o in packet["prior_observations"]}
        dependencies.update(
            p["task_id"] for p in dependency_packets if ids & set(p["scope"]["target_comment_ids"])
        )
        assembly.packets.append(packet)
        assembly.task_rows.append(
            {
                "task_id": packet["task_id"],
                "task_type": task_type,
                "phase": phase,
                "synthesis_level": synthesis_level,
                "target_uid": target_uid,
                "root_ids": packet["scope"]["root_ids"],
                "input_path": f"tasks/{packet['task_id']}/input.json",
                "input_sha256": sha(json_bytes(packet)),
                "depends_on": sorted(dependencies),
            }
        )
        published.append(
            {
                "task_id": packet["task_id"],
                "index": index_number,
                "target_comment_ids": list(packet["scope"]["target_comment_ids"]),
            }
        )
    assembly.target_indexes[index_path] = index_rows
    group = {
        "group_id": group_id,
        "task_type": task_type,
        "phase": phase,
        "synthesis_level": synthesis_level,
        "target_uid": target_uid,
        "root_ids": sorted({rows[cid]["root_id"] for cid in targets}, key=int),
        "target_index": index,
        "published_tasks": published,
        "pending_targets": pending,
        "final_merge_task_id": packets[0]["task_id"]
        if task_type == "user_synthesis" and len(packets) == 1 and not pending
        else None,
    }
    assembly.group_coverage["groups"].append(group)
    return group


def build_primary(bundle, run_id, resources, budget, *, resource_files):
    assembly = new_assembly(bundle, run_id, resources, resource_files, budget)
    for uid in sorted(bundle._data["users"], key=int):
        add_group(
            assembly,
            bundle,
            task_type="user_initial",
            phase="primary",
            target_uid=uid,
            root_ids=(),
            synthesis_level=None,
            budget=budget,
        )
    for root in sorted(bundle._data["threads"], key=int):
        add_group(
            assembly,
            bundle,
            task_type="thread_context",
            phase="primary",
            target_uid=None,
            root_ids=(root,),
            synthesis_level=None,
            budget=budget,
        )
    return assembly
