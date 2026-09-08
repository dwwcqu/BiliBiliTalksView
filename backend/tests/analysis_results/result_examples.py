from app.analysis_packets.builder import DIMENSIONS


def response_for(packet, attempt_id):
    from app.analysis_packets.builder import json_bytes, sha

    uids = sorted(
        {
            c["author_uid"]
            for c in packet["comments"]
            if c["input_role"] == "target" and c["author_uid"] is not None
        },
        key=int,
    )
    return {
        "protocol": "BiliBiliTalksView.AnalysisOutput",
        "schema_version": "1.0.0",
        "artifact_type": "task_result",
        **{
            k: packet[k]
            for k in (
                "task_id",
                "run_id",
                "video_id",
                "export_id",
                "task_type",
                "phase",
                "synthesis_level",
            )
        },
        "attempt_id": attempt_id,
        "target_uid": packet["scope"]["target_uid"],
        "input_sha256": sha(json_bytes(packet)),
        "rules_sha256": packet["resources"]["analysis_rules"]["sha256"],
        "model_status": "completed",
        "coverage": {
            "processed_comment_ids": packet["scope"]["target_comment_ids"][:],
            "unprocessed_targets": [],
            "limitations": [],
        },
        "observations": [],
        "dimension_results": [
            {
                "uid": uid,
                "dimension": d,
                "assessment_status": "insufficient",
                "observation_ids": [],
                "limitations": ["合成未作出判断"],
            }
            for uid in uids
            for d in DIMENSIONS
        ],
        "prior_dispositions": [
            {
                "prior_observation_id": o["observation_id"],
                "origin_task_id": o["origin_task_id"],
                "decision": "unresolved",
                "replacement_observation_ids": [],
                "rationale": "合成未作出判断",
                "evidence": [],
            }
            for o in packet["prior_observations"]
        ],
        "summary": "本批输入未作出确定判断。",
        "limitations": ["合成响应，不是模型评估。"],
    }


def register_members(assembly):
    from uuid import uuid4

    main = assembly.member_registry["main"].get("session_id") or str(uuid4())
    assembly.member_registry["registry_id"] = str(uuid4())
    assembly.member_registry["main"] = {"session_id": main, "status": "available"}
    assembly.member_registry["members"] = [
        {
            "member_id": "worker-" + role,
            "agent_id": "synthetic-" + role,
            "parent_session_id": main,
            "role": role,
            "status": "available",
            "active_task_id": None,
            "task_ids": [p["task_id"] for p in assembly.packets if p["task_type"] == role],
            "role_sha256": assembly.resources["role_prompt"]["sha256"],
        }
        for role in ("user_initial", "thread_context", "user_synthesis")
    ]
    return assembly


def ensure_execution(case):
    from pathlib import Path

    from app.analysis_packets.builder import json_bytes, sha

    assembly = case["assembly"]
    raw = json_bytes(
        {
            "run_id": assembly.run_id,
            "rules_sha256": case["rules"]["rules_sha256"],
            "cli_version": "synthetic",
            "configured_model": "synthetic",
            "reported_model": None,
            "provider": "synthetic",
            "session_id": assembly.member_registry["main"]["session_id"],
            "agent_ids": [m["agent_id"] for m in assembly.member_registry["members"]],
            "input_protocol_version": "2.0.0",
            "output_protocol_version": "1.0.0",
            "limits": assembly.limits,
        }
    )
    path = Path(case["publication"]["run_path"]) / "execution.json"
    if path.exists():
        assert path.read_bytes() == raw
    else:
        path.write_bytes(raw)
    return {"path": "execution.json", "sha256": sha(raw)}


def execution_for(case, packet, attempt_id):
    from app.analysis_packets.builder import json_bytes, sha
    from app.analysis_results.types import ExecutionContext

    return ExecutionContext(
        attempt_id,
        sha(json_bytes(packet)),
        True,
        True,
        ensure_execution(case),
        "synthetic-" + packet["task_type"],
    )
