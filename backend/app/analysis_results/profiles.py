"""Publish UID profiles exclusively from revalidated, frozen accepted results."""

import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.analysis_input.locking import preparation_lock
from app.analysis_input.storage import safe_child
from app.analysis_packets.builder import DIMENSIONS, json_bytes, sha
from app.analysis_packets.codec import loads
from app.comment_export.export import nickname

from .acceptance import PROTOCOL, VERSION, _write_once, load_context, load_task_rows, read_state
from .contract import validate_document
from .errors import OutputError

CANDIDATE_SUMMARY = "分析尚未完成；当前仅保存已接受任务的阶段观察，不构成最终用户画像。"


def _execution(run: Path, reference: dict) -> dict:
    try:
        path = reference["path"]
        if path != "execution.json":
            raise ValueError("unbound_execution")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or re.search(r"[\\:\x00-\x1f]", path)
        ):
            raise ValueError
        raw = safe_child(run, path).read_bytes()
        if sha(raw) != reference["sha256"] or raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
            raise ValueError
        value = loads(raw.decode("utf-8"))
        strings = (
            "cli_version",
            "configured_model",
            "provider",
            "input_protocol_version",
            "output_protocol_version",
        )
        if any(not isinstance(value[k], str) or not value[k].strip() for k in strings):
            raise ValueError
        if any(
            value[k] is not None and (not isinstance(value[k], str) or not value[k].strip())
            for k in ("reported_model", "session_id")
        ):
            raise ValueError
        if (
            not isinstance(value["agent_ids"], list)
            or any(not isinstance(v, str) or not v.strip() for v in value["agent_ids"])
            or len(set(value["agent_ids"])) != len(value["agent_ids"])
            or not isinstance(value["limits"], dict)
            or value["input_protocol_version"] != "2.0.0"
            or value["output_protocol_version"] != VERSION
        ):
            raise ValueError
        return {"path": path, "sha256": sha(raw)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise OutputError("invalid_execution") from None


def _coverage(bundle, assembly, catalog, uid, history_rows):
    targets = set(bundle.users[uid])
    roots = {bundle.comments_by_id[cid]["root_id"] for cid in targets}
    groups = [
        g
        for g in assembly.group_coverage["groups"]
        if g["target_uid"] == uid
        or (g["task_type"] == "thread_context" and roots.intersection(g["root_ids"]))
    ]
    required = {p["task_id"] for g in groups for p in g["published_tasks"]}
    rows = history_rows | {r["task_id"]: r for r in assembly.task_rows}
    pending = list(required)
    while pending:
        for dependency in rows[pending.pop()]["depends_on"]:
            if dependency not in required:
                required.add(dependency)
                pending.append(dependency)
    packets = {task: result.packet for task, result in catalog.items()} | {
        p["task_id"]: p for p in assembly.packets
    }
    accepted = required.intersection(catalog)
    processed = targets.intersection(
        cid
        for task in accepted
        for cid in loads(catalog[task].result_bytes.decode("utf-8"))["coverage"][
            "processed_comment_ids"
        ]
    )
    missing = required - accepted
    synthesis = [g for g in groups if g["task_type"] == "user_synthesis"]
    highest = max((g["synthesis_level"] for g in synthesis), default=-1)
    finalists = [
        g["final_merge_task_id"]
        for g in synthesis
        if g["synthesis_level"] == highest and g["final_merge_task_id"] is not None
    ]
    final = finalists[0] if len(finalists) == 1 else None
    stages_present = any(g["task_type"] == "user_initial" for g in groups) and all(
        any(
            g["task_type"] == "thread_context" and g["phase"] == phase and root in g["root_ids"]
            for g in groups
        )
        for root in roots
        for phase in ("primary", "reconcile")
    )
    gaps = any(g["pending_targets"] for g in groups) or any(
        gap["kind"] == "prior_result_missing"
        for task in required
        for gap in packets[task]["coverage"]["context_gaps"]
    )
    complete = bool(
        stages_present
        and final in accepted
        and not missing
        and not gaps
        and processed == targets
        and set(packets[final]["scope"]["target_comment_ids"]) == targets
    )
    limitations = []
    if not complete:
        limitations.append(CANDIDATE_SUMMARY)
    if gaps:
        limitations.append("存在尚未解决的任务目标或前序结果缺口。")
    if not stages_present:
        limitations.append("必需的初步分析或相关楼 primary/reconcile 阶段尚未发布。")
    coverage = {
        "status": "complete" if complete else "partial",
        "target_comment_ids": sorted(targets, key=int),
        "processed_comment_ids": sorted(processed, key=int),
        "unprocessed_targets": [
            {
                "comment_id": cid,
                "reason": "dependency_unavailable",
                "detail": "尚无已接受任务处理此目标。",
            }
            for cid in sorted(targets - processed, key=int)
        ],
        "required_task_ids": sorted(required),
        "accepted_task_ids": sorted(accepted),
        "missing_task_ids": sorted(missing),
    }
    return coverage, final if complete else None, limitations


def _dimensions(catalog, tasks, uid, *, final=False):
    dimensions = []
    for dimension in DIMENSIONS:
        observations, limitations, statuses = [], [], []
        for task in tasks:
            result = catalog[task]
            value = loads(result.result_bytes.decode("utf-8"))
            for item in value["dimension_results"]:
                if item["uid"] == uid and item["dimension"] == dimension:
                    limitations.extend(item["limitations"])
                    statuses.append(item["assessment_status"])
            for observation in value["observations"]:
                if observation["uid"] == uid and observation["dimension"] == dimension:
                    observations.append(
                        deepcopy(observation)
                        | {
                            "origin_result": {
                                "path": result.result_path,
                                "sha256": sha(result.result_bytes),
                            }
                        }
                    )
        states = {o["assessment_status"] for o in observations}
        status = next(
            (
                s
                for s in ("assessable", "ambiguous", "insufficient", "not_applicable")
                if s in states
            ),
            "insufficient",
        )
        if not observations:
            if final and statuses and all(s == "not_applicable" for s in statuses):
                status = "not_applicable"
            if not limitations:
                limitations.append("尚无已接受观察可用于此方向。")
        dimensions.append(
            {
                "dimension": dimension,
                "assessment_status": status,
                "observations": observations,
                "limitations": list(dict.fromkeys(limitations)),
            }
        )
    return dimensions


def save_profile(root, run_id, manifest_id, uid, rule_catalog, execution_ref) -> dict:
    """Save a final profile or independent candidate using trusted execution provenance."""
    run, _, _, manifest = load_context(root, run_id, manifest_id)
    with preparation_lock(run / ".results.lock"):
        run, bundle, assembly, catalog = read_state(root, run_id, manifest_id, rule_catalog)
        if not isinstance(uid, str) or uid not in bundle.users:
            raise OutputError("unknown_uid")
        execution = _execution(run, execution_ref)
        history_rows = load_task_rows(run, manifest)
        coverage, final, limitations = _coverage(bundle, assembly, catalog, uid, history_rows)
        provenance = loads(safe_child(run, execution["path"]).read_text(encoding="utf-8"))
        if provenance.get("reported_model_scope") == "main_session_only":
            limitations.append("实际模型仅由主会话确认；子成员实际模型未独立核验。")
        source = bundle.manifest
        if (
            source["coverage"]["status"] != "verified"
            or source["coverage"]["context_status"] == "gaps"
        ):
            limitations.append("源采集存在缺口；分析覆盖仅表示本批已取得评论的处理状态。")
        tasks = coverage["accepted_task_ids"]
        source_results = []
        for task in tasks:
            result = catalog[task]
            value = loads(result.result_bytes.decode("utf-8"))
            limitations.extend(value["limitations"])
            limitations.extend(value["coverage"]["limitations"])
            limitations.extend(result.packet["coverage"]["limitations"])
            source_results.append(
                {
                    "task_id": task,
                    "attempt_id": value["attempt_id"],
                    "path": result.result_path,
                    "sha256": sha(result.result_bytes),
                }
            )
        context = {
            kind: {k: reference[k] for k in ("path", "sha256")}
            | {"version": reference.get("version", reference["sha256"])}
            for kind, reference in assembly.resources.items()
        }
        context["execution_ref"] = execution
        artifact_type = "user_profile" if final else "profile_candidate"
        profile = {
            "protocol": PROTOCOL,
            "schema_version": VERSION,
            "artifact_type": artifact_type,
            "video_id": bundle.video_id,
            "uid": uid,
            "display_nickname": nickname([bundle.comments_by_id[cid] for cid in bundle.users[uid]]),
            "video_title": source["title"],
            "run_id": run_id,
            "prepared_run_id": bundle.prepared_run_id,
            "export_id": bundle.export_id,
            "export_schema_version": bundle.export_schema_version,
            "input_fingerprint": bundle.input_fingerprint,
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "context": context,
            "source_coverage": source["coverage"],
            "corpus_counts": source["counts"],
            "analysis_coverage": coverage,
            "summary": loads(catalog[final].result_bytes.decode("utf-8"))["summary"]
            if final
            else CANDIDATE_SUMMARY,
            "dimensions": _dimensions(
                catalog, [final] if final else tasks, uid, final=final is not None
            ),
            "source_results": source_results,
            "limitations": list(dict.fromkeys(limitations)),
        }
        validate_document("user_profile", profile)
        relative = f"users/{uid}.json" if final else (f"intermediate/profiles/{uid}/{uuid4()}.json")
        path = safe_child(run, relative)
        if path.exists():
            try:
                previous = loads(path.read_text(encoding="utf-8"))
                validate_document("user_profile", previous)
                expected = profile | {"created_at": previous["created_at"]}
                if previous != expected:
                    raise ValueError
            except (OSError, ValueError, KeyError, TypeError):
                raise OutputError("profile_conflict") from None
        else:
            _write_once(path, json_bytes(profile))
        return {"path": relative, "artifact_type": artifact_type, "analysis_coverage": coverage}
