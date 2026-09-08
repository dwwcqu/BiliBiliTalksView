"""Cross-document checks for offline packets and explicitly accepted frozen results."""

from collections import Counter
from pathlib import PurePosixPath

from .budget import METHOD, estimate_input
from .builder import json_bytes, jsonl_bytes, sha
from .codec import loads as parse_json
from .context import select_context
from .contract import validate_document
from .errors import PacketError
from .source import SourceBundle
from .types import AcceptedCatalog, Assembly


def require(condition, code):
    if not condition:
        raise PacketError(code)


def safe_reference(path):
    require(
        isinstance(path, str)
        and bool(path)
        and "\\" not in path
        and ":" not in path
        and not PurePosixPath(path).is_absolute()
        and all(part not in {"", ".", ".."} for part in path.split("/")),
        "invalid_reference_path",
    )


def _identity(packet, assembly, bundle):
    for key, expected in {
        "run_id": assembly.run_id,
        "video_id": bundle.video_id,
        "export_id": bundle.export_id,
        "export_schema_version": bundle.export_schema_version,
        "prepared_run_id": bundle.prepared_run_id,
    }.items():
        require(packet[key] == expected, "packet_identity_mismatch")
    require(packet["resources"] == assembly.resources, "resource_mismatch")


def _resources(assembly, bundle):
    frozen = bundle.context_bytes
    require(
        set(assembly.resources) == {"background", "analysis_rules", "role_prompt", "coordination"},
        "invalid_resource_set",
    )
    paths = [reference["path"] for reference in assembly.resources.values()]
    require(
        set(assembly.resource_files) == set(paths)
        and len({path.casefold() for path in paths}) == 4,
        "invalid_resource_set",
    )
    for key, reference in assembly.resources.items():
        path = reference["path"]
        safe_reference(path)
        parts = path.split("/")
        require(
            len(parts) == 2
            and parts[0] == "context"
            and parts[1] in frozen
            and not parts[1].endswith((" ", "."))
            and not any(ord(char) < 32 or char in '<>"|?*' for char in parts[1]),
            "invalid_resource_path",
        )
        basename = parts[1]
        stem = basename.split(".")[0].casefold()
        reserved = {"con", "prn", "aux", "nul"} | {
            f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
        }
        require(stem not in reserved, "invalid_resource_path")
        require(path in assembly.resource_files, "resource_missing")
        data = assembly.resource_files[path]
        require(sha(data) == reference["sha256"], "resource_hash_mismatch")
        require(data == frozen[basename], "resource_not_frozen")
        if key == "background":
            require(
                data == frozen["README.md"] and data.decode("utf-8") == reference["text"],
                "background_mismatch",
            )


def _packet_source(packet, bundle):
    source = bundle._data["comments"]
    scope = packet["scope"]
    targets, context = set(scope["target_comment_ids"]), set(scope["context_comment_ids"])
    require(
        targets and not targets & context and targets | context <= source.keys(), "invalid_scope"
    )
    require(
        set(scope["root_ids"]) == {source[cid]["root_id"] for cid in targets},
        "target_roots_mismatch",
    )
    uid = scope["target_uid"]
    if packet["task_type"] != "thread_context":
        require(
            uid in bundle._data["users"]
            and all(source[cid]["author"]["uid"] == uid for cid in targets),
            "target_uid_mismatch",
        )
    visible = {row["comment_id"]: row for row in packet["comments"]}
    require(len(visible) == len(packet["comments"]), "duplicate_visible_comment")
    require(set(visible) <= targets | context and context <= visible.keys(), "unregistered_comment")
    if packet["task_type"] != "user_synthesis":
        require(targets <= visible.keys(), "missing_target_text")
    else:
        covered = {cid for o in packet["prior_observations"] for cid in o["source_comment_ids"]}
        require(targets <= visible.keys() | covered, "missing_synthesis_coverage")
    for cid, row in visible.items():
        original = source[cid]
        for key in (
            "comment_id",
            "root_id",
            "parent_id",
            "kind",
            "reply_relation",
            "content",
            "created_at",
            "collected_at",
        ):
            require(json_bytes(row[key]) == json_bytes(original[key]), "source_projection_mismatch")
        require(row["author_uid"] == original["author"]["uid"], "source_projection_mismatch")
        target = cid in targets
        require(
            row["input_role"] == ("target" if target else "context")
            and ("target" in row["context_reasons"]) == target,
            "input_role_mismatch",
        )
    ordered = sorted(
        packet["comments"],
        key=lambda r: (
            int(r["root_id"]),
            r["kind"] != "root",
            r["created_at"] is None,
            r["created_at"] or "",
            int(r["comment_id"]),
        ),
    )
    require(packet["comments"] == ordered, "comment_order_mismatch")
    coverage = packet["coverage"]
    require(
        json_bytes(coverage["source"]) == json_bytes(bundle._data["manifest"]["coverage"])
        and json_bytes(coverage["corpus_counts"]) == json_bytes(bundle._data["manifest"]["counts"]),
        "source_coverage_mismatch",
    )
    omitted = set(coverage["omitted_context_ids"])
    require(
        omitted <= source.keys() and not omitted & (targets | visible.keys()),
        "invalid_omitted_context",
    )
    gap_ids = {
        cid
        for gap in coverage["context_gaps"]
        if gap["kind"] == "context_budget"
        for cid in gap["comment_ids"]
    }
    require(omitted <= gap_ids, "missing_context_budget_gap")
    evidence_ids = {
        e["comment_id"]
        for o in packet["prior_observations"]
        for e in [*o["evidence"], *o["counter_evidence"]]
    }
    evidence_ids.update(
        cid for o in packet["prior_observations"] for cid in o["subject"]["evidence_comment_ids"]
    )
    selected = tuple(sorted((targets & visible.keys()) | evidence_ids, key=int))
    expected_context, expected_gaps = select_context(bundle, selected)
    require(
        {r["comment_id"] for r in expected_context} <= visible.keys() | omitted,
        "missing_required_context",
    )
    for expected in expected_gaps:
        require(
            any(
                gap["kind"] == expected["kind"]
                and set(expected["comment_ids"]) <= set(gap["comment_ids"])
                and set(expected["root_ids"]) <= set(gap["root_ids"])
                for gap in coverage["context_gaps"]
            ),
            "missing_context_gap",
        )
    require(not packet["prior_observations"] or coverage["summary_used"], "summary_flag_missing")
    if packet["task_type"] == "user_synthesis" and packet["synthesis_level"] > 0:
        require(coverage["summary_used"], "summary_flag_missing")


def _accepted(task_id, accepted, assembly, bundle):
    require(task_id in accepted, "unaccepted_result")
    result = accepted[task_id]
    require(result.status == "accepted", "unaccepted_result")
    packet = result.packet
    validate_document("packet", packet)
    _identity(packet, assembly, bundle)
    _packet_source(packet, bundle)
    require(packet["task_id"] == task_id, "result_identity_mismatch")
    safe_reference(result.result_path)
    document = parse_json(result.result_bytes.decode("utf-8"))
    for key in ("task_id", "run_id", "video_id", "export_id", "synthesis_level"):
        require(json_bytes(document[key]) == json_bytes(packet[key]), "result_identity_mismatch")
    require(
        document["target_uid"] == packet["scope"]["target_uid"]
        and document["rules_sha256"] == assembly.resources["analysis_rules"]["sha256"]
        and json_bytes(document["observations"]) == json_bytes(list(result.observations)),
        "result_identity_mismatch",
    )
    return result


def _observation(observation, packet, dependencies, accepted, assembly, bundle):
    validate_document("observation", observation)
    origin = observation["origin_task_id"]
    require(origin in dependencies, "undeclared_result_dependency")
    result = _accepted(origin, accepted, assembly, bundle)
    source_packet = result.packet
    require(
        observation["origin_result"]
        == {"path": result.result_path, "sha256": sha(result.result_bytes)},
        "origin_result_mismatch",
    )
    # The output cannot contain its own digest. origin_result is bound by the assembler.
    comparable = {k: v for k, v in observation.items() if k != "origin_result"}
    require(
        any(
            json_bytes(comparable)
            == json_bytes({k: v for k, v in candidate.items() if k != "origin_result"})
            for candidate in result.observations
        ),
        "observation_not_accepted",
    )
    require(
        observation["origin_type"] == source_packet["task_type"]
        and observation["export_id"] == bundle.export_id
        and observation["rules_sha256"] == assembly.resources["analysis_rules"]["sha256"],
        "observation_identity_mismatch",
    )
    if observation["assessment_status"] == "assessable":
        require(bool(observation["evidence"]), "missing_observation_evidence")
    uid = observation["uid"]
    source_ids = set(observation["source_comment_ids"])
    require(
        uid in bundle._data["users"]
        and source_ids
        and source_ids <= set(source_packet["scope"]["target_comment_ids"]),
        "observation_scope_mismatch",
    )
    require(
        any(bundle._data["comments"][cid]["author"]["uid"] == uid for cid in source_ids),
        "observation_uid_mismatch",
    )
    if source_packet["scope"]["target_uid"] is not None:
        require(source_packet["scope"]["target_uid"] == uid, "observation_uid_mismatch")
    if packet["task_type"] == "user_synthesis":
        require(uid == packet["scope"]["target_uid"], "observation_uid_mismatch")
    visible = {r["comment_id"]: r for r in packet["comments"]}
    origin_visible = {r["comment_id"] for r in source_packet["comments"]}
    for evidence in [*observation["evidence"], *observation["counter_evidence"]]:
        cid, quote = evidence["comment_id"], evidence["quote"]
        require(cid in visible and cid in origin_visible, "missing_evidence_text")
        text = visible[cid]["content"]["text"]
        require(isinstance(text, str) and bool(quote) and quote in text, "invalid_evidence_quote")
    subject = observation["subject"]
    require(
        set(subject["evidence_comment_ids"]) <= visible.keys()
        and set(subject["evidence_comment_ids"]) <= origin_visible,
        "missing_subject_evidence",
    )
    if subject["kind"] == "unknown" and observation["dimension"] in {
        "topic_stance",
        "expressed_emotion",
        "view_revision",
    }:
        require(
            observation["assessment_status"] in {"ambiguous", "insufficient"}, "unlocated_subject"
        )
    if subject["kind"] != "unknown":
        require(bool(subject["evidence_comment_ids"]), "missing_subject_evidence")
    if subject["target_uid"] is not None:
        target = subject["target_uid"]
        require(
            subject["kind"] == "participant"
            and target in bundle._data["users"]
            and any(
                visible[cid]["author_uid"] == target
                or visible[cid]["reply_relation"]["target_uid"] == target
                for cid in subject["evidence_comment_ids"]
            ),
            "subject_uid_unproven",
        )


def _dependencies(packet, row, packets, accepted, assembly, bundle):
    dependencies = row["depends_on"]
    if packet["task_type"] in {"user_initial", "thread_context"} and packet["phase"] == "primary":
        require(not packet["prior_observations"], "primary_observations_forbidden")
    for observation in packet["prior_observations"]:
        _observation(observation, packet, dependencies, accepted, assembly, bundle)
    targets = set(packet["scope"]["target_comment_ids"])
    if packet["phase"] == "reconcile":
        primaries = [
            p
            for p in packets.values()
            if p["task_type"] == "thread_context"
            and p["phase"] == "primary"
            and targets & set(p["scope"]["target_comment_ids"])
        ]
        require(
            targets <= {cid for p in primaries for cid in p["scope"]["target_comment_ids"]}
            and all(p["task_id"] in dependencies for p in primaries),
            "missing_primary_dependency",
        )
        for primary in primaries:
            _accepted(primary["task_id"], accepted, assembly, bundle)
    if packet["task_type"] == "user_synthesis":
        level, uid = packet["synthesis_level"], packet["scope"]["target_uid"]
        sources = [_accepted(task, accepted, assembly, bundle).packet for task in dependencies]
        for source in sources:
            if level > 0:
                require(
                    source["task_type"] == "user_synthesis"
                    and source["synthesis_level"] == level - 1
                    and source["scope"]["target_uid"] == uid,
                    "invalid_synthesis_dependency",
                )
            else:
                require(
                    (source["task_type"] == "user_initial" and source["scope"]["target_uid"] == uid)
                    or (source["task_type"] == "thread_context" and source["phase"] == "reconcile"),
                    "invalid_synthesis_dependency",
                )
        if level > 0:
            require(
                targets <= {cid for p in sources for cid in p["scope"]["target_comment_ids"]},
                "missing_synthesis_dependency",
            )
            inherited = [
                gap
                for source in sources
                for gap in source["coverage"]["context_gaps"]
                if gap["kind"] == "prior_result_missing"
            ]
            require(
                all(
                    any(
                        gap["kind"] == old["kind"]
                        and set(old["comment_ids"]) <= set(gap["comment_ids"])
                        for gap in packet["coverage"]["context_gaps"]
                    )
                    for old in inherited
                ),
                "missing_prior_result_gap",
            )
        else:
            reconciled = {
                cid
                for source in sources
                if source["task_type"] == "thread_context"
                for cid in source["scope"]["target_comment_ids"]
            }
            missing = targets - reconciled
            declared = {
                cid
                for gap in packet["coverage"]["context_gaps"]
                if gap["kind"] == "prior_result_missing"
                for cid in gap["comment_ids"]
            }
            require(missing <= declared, "missing_prior_result_gap")


def _groups(assembly, bundle, packets):
    ledger = assembly.group_coverage
    validate_document("group-coverage", ledger)
    require(
        ledger["run_id"] == assembly.run_id and ledger["export_id"] == bundle.export_id,
        "group_identity_mismatch",
    )
    assigned, group_ids, logical, floor_groups = set(), set(), set(), {}
    source = bundle._data["comments"]
    for group in ledger["groups"]:
        group_id = group["group_id"]
        require(group_id not in group_ids, "duplicate_group")
        group_ids.add(group_id)
        key = tuple(group[k] for k in ("task_type", "phase", "synthesis_level", "target_uid"))
        if group["target_uid"] is not None:
            require(key not in logical, "duplicate_logical_group")
            logical.add(key)
        else:
            roots = floor_groups.setdefault(key, set())
            require(not roots & set(group["root_ids"]), "duplicate_logical_group")
            roots.update(group["root_ids"])
        reference = group["target_index"]
        safe_reference(reference["path"])
        rows = assembly.target_indexes[reference["path"]]
        ids = [r["comment_id"] for r in rows]
        require(
            len(set(ids)) == len(ids)
            and reference["count"] == len(ids)
            and reference["sha256"] == sha(jsonl_bytes(rows)),
            "target_index_mismatch",
        )
        for row in rows:
            validate_document("target-index-row", row)
            original = source[row["comment_id"]]
            require(
                row["root_id"] == original["root_id"]
                and row["author_uid"] == original["author"]["uid"],
                "target_index_mismatch",
            )
        expected = (
            set(bundle._data["users"].get(group["target_uid"], ()))
            if group["target_uid"] is not None
            else {
                cid for root in group["root_ids"] for cid in bundle._data["threads"].get(root, ())
            }
        )
        require(
            set(ids) == expected
            and set(group["root_ids"]) == {source[cid]["root_id"] for cid in ids},
            "incomplete_target_index",
        )
        published = group["published_tasks"]
        partition = [p["comment_id"] for p in group["pending_targets"]]
        for index, item in enumerate(published):
            task = item["task_id"]
            require(task in packets and task not in assigned, "invalid_published_task")
            assigned.add(task)
            packet = packets[task]
            require(
                item["index"] == index
                and packet["chunk"]["index"] == index
                and packet["chunk"]["count"] == len(published)
                and packet["chunk"]["group_id"] == group_id
                and item["target_comment_ids"] == packet["scope"]["target_comment_ids"]
                and packet["scope"]["target_index"] == reference,
                "chunk_group_mismatch",
            )
            require(
                tuple(packet[k] for k in ("task_type", "phase", "synthesis_level")) == key[:3]
                and packet["scope"]["target_uid"] == group["target_uid"],
                "group_identity_mismatch",
            )
            partition.extend(item["target_comment_ids"])
        require(Counter(partition) == Counter(ids), "invalid_target_partition")
        final = group["final_merge_task_id"]
        if final is not None:
            require(
                group["task_type"] == "user_synthesis"
                and len(published) == 1
                and not group["pending_targets"]
                and final == published[0]["task_id"],
                "invalid_final_merge",
            )
    require(assigned == packets.keys(), "unregistered_packet")


def _validate(assembly, bundle, accepted, execution):
    _resources(assembly, bundle)
    schema_refs = [
        p["response_contract"]["schema_ref"]
        for p in assembly.packets
        if p["response_contract"]["schema_ref"] is not None
    ]
    require(
        set(assembly.output_schemas) == {ref["path"] for ref in schema_refs},
        "output_schema_set_mismatch",
    )
    for ref in schema_refs:
        safe_reference(ref["path"])
        require(
            ref["path"].startswith("schemas/")
            and sha(assembly.output_schemas[ref["path"]]) == ref["sha256"],
            "output_schema_mismatch",
        )

    packets = {p["task_id"]: p for p in assembly.packets}
    rows = {r["task_id"]: r for r in assembly.task_rows}
    require(
        len(packets) == len(assembly.packets)
        and len(rows) == len(assembly.task_rows)
        and packets.keys() == rows.keys(),
        "duplicate_or_missing_task",
    )
    all_packets = {task: result.packet for task, result in accepted.items()} | packets
    for task in accepted.keys() & packets.keys():
        require(
            json_bytes(accepted[task].packet) == json_bytes(packets[task]),
            "accepted_packet_changed",
        )
    loaded = b"".join(
        assembly.resource_files[assembly.resources[k]["path"]]
        for k in ("analysis_rules", "role_prompt", "coordination")
    )
    for task, packet in packets.items():
        validate_document("packet", packet, execution=execution)
        _identity(packet, assembly, bundle)
        _packet_source(packet, bundle)
        row = rows[task]
        validate_document("task-index-row", row)
        require(
            row["input_path"] == f"tasks/{task}/input.json"
            and row["input_sha256"] == sha(json_bytes(packet)),
            "task_input_mismatch",
        )
        require(
            all(row[k] == packet[k] for k in ("task_type", "phase", "synthesis_level"))
            and row["target_uid"] == packet["scope"]["target_uid"]
            and row["root_ids"] == packet["scope"]["root_ids"],
            "task_index_identity_mismatch",
        )
        chunk = packet["chunk"]
        require(
            chunk["estimator"] == METHOD
            and chunk["estimated_input_tokens"] == estimate_input(json_bytes(packet), loaded)
            and chunk["estimated_input_tokens"] <= chunk["input_token_limit"]
            and chunk["input_token_limit"] == assembly.limits["max_input_tokens"]
            and chunk["reserved_output_tokens"] == assembly.limits["reserved_output_tokens"]
            and chunk["input_token_limit"] + chunk["reserved_output_tokens"]
            <= assembly.context_window,
            "input_budget_mismatch",
        )
        require(set(row["depends_on"]) <= all_packets.keys(), "unknown_dependency")
        for dependency in row["depends_on"]:
            _identity(all_packets[dependency], assembly, bundle)
            if dependency not in packets:
                _accepted(dependency, accepted, assembly, bundle)
        _dependencies(packet, row, all_packets, accepted, assembly, bundle)
    visiting, visited = set(), set()

    def visit(task):
        require(task not in visiting, "dependency_cycle")
        if task in visited:
            return
        visiting.add(task)
        for dep in rows.get(task, {}).get("depends_on", []):
            visit(dep)
        visiting.remove(task)
        visited.add(task)

    for task in rows:
        visit(task)
    _groups(assembly, bundle, packets)
    registry = assembly.member_registry
    validate_document("member-registry", registry)
    require(
        registry["run_id"] == assembly.run_id and registry["video_id"] == bundle.video_id,
        "registry_identity_mismatch",
    )
    require(
        len({m["member_id"] for m in registry["members"]}) == len(registry["members"])
        and len({m["agent_id"] for m in registry["members"]}) == len(registry["members"]),
        "duplicate_member",
    )
    for member in registry["members"]:
        require(
            member["parent_session_id"] == registry["main"]["session_id"]
            and set(member["task_ids"]) <= all_packets.keys()
            and (
                member["active_task_id"] is None or member["active_task_id"] in member["task_ids"]
            ),
            "member_task_mismatch",
        )


def validate_assembly(
    assembly: Assembly, bundle: SourceBundle, accepted: AcceptedCatalog, *, execution: bool = False
) -> None:
    """Validate without network, files, model calls, or trusting self-declared completion."""
    try:
        _validate(assembly, bundle, accepted, execution)
    except PacketError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, UnicodeError, RecursionError):
        raise PacketError("invalid_assembly") from None
