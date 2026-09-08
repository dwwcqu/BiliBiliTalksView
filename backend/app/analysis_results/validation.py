"""Validate task-local identities, coverage, evidence and prior dispositions."""

import re
from collections import Counter

from .contract import DIMENSIONS, validate_document
from .errors import OutputError


def require(condition, code):
    if not condition:
        raise OutputError(code)


def aggregate_status(observations):
    statuses = {item["assessment_status"] for item in observations}
    return next(
        (s for s in ("assessable", "ambiguous", "insufficient") if s in statuses), "not_applicable"
    )


def _quotes(items, visible):
    for item in items:
        cid, quote = item["comment_id"], item["quote"]
        require(cid in visible, "missing_evidence_text")
        text = visible[cid]["content"]["text"]
        require(
            isinstance(text, str) and bool(quote.strip()) and quote in text,
            "invalid_evidence_quote",
        )


def _validate(value, packet, rule_catalog, attempt_id, input_sha256):
    validate_document("task_result", value)
    for key in (
        "task_id",
        "run_id",
        "video_id",
        "export_id",
        "task_type",
        "phase",
        "synthesis_level",
    ):
        require(value[key] == packet[key], "result_identity_mismatch")
    require(
        value["attempt_id"] == attempt_id
        and value["input_sha256"] == input_sha256
        and value["target_uid"] == packet["scope"]["target_uid"],
        "result_identity_mismatch",
    )
    rules_sha = packet["resources"]["analysis_rules"]["sha256"]
    require(value["rules_sha256"] == rules_sha, "rules_hash_mismatch")
    require(
        isinstance(rule_catalog, dict) and rule_catalog.get("rules_sha256") == rules_sha,
        "rule_catalog_mismatch",
    )
    labels = rule_catalog.get("labels")
    require(
        isinstance(labels, dict)
        and set(DIMENSIONS) <= labels.keys()
        and all(
            isinstance(labels[d], list)
            and all(isinstance(s, str) and bool(s.strip()) for s in labels[d])
            for d in DIMENSIONS
        ),
        "rule_catalog_missing",
    )
    targets = set(packet["scope"]["target_comment_ids"])
    processed = set(value["coverage"]["processed_comment_ids"])
    remaining_list = [item["comment_id"] for item in value["coverage"]["unprocessed_targets"]]
    remaining = set(remaining_list)
    require(
        targets
        and len(remaining) == len(remaining_list)
        and not processed & remaining
        and processed | remaining == targets,
        "invalid_target_partition",
    )
    status = "completed" if not remaining else ("partial" if processed else "unable")
    require(value["model_status"] == status, "model_status_mismatch")
    visible = {item["comment_id"]: item for item in packet["comments"]}
    require(len(visible) == len(packet["comments"]), "duplicate_visible_comment")
    # Synthesis may omit target text already covered by prior observations. Its explicit
    # target UID supplies ownership; thread tasks require visible original authors.
    target_uid = packet["scope"]["target_uid"]
    owners = {cid: visible[cid]["author_uid"] if cid in visible else target_uid for cid in targets}
    require(
        all(cid in visible or target_uid is not None for cid in targets), "missing_target_identity"
    )
    uids = {uid for uid in owners.values() if uid is not None}
    observations = {}
    for observation in value["observations"]:
        oid, uid = observation["observation_id"], observation["uid"]
        require(
            re.fullmatch(re.escape(value["task_id"]) + r":o[1-9][0-9]*", oid)
            and oid not in observations,
            "invalid_observation_identity",
        )
        require(
            observation["origin_task_id"] == value["task_id"]
            and observation["origin_type"] == value["task_type"]
            and observation["export_id"] == value["export_id"]
            and observation["rules_sha256"] == rules_sha,
            "observation_identity_mismatch",
        )
        require(
            uid in uids and (target_uid is None or uid == target_uid), "observation_uid_mismatch"
        )
        own_targets = {cid for cid, owner in owners.items() if owner == uid}
        source = set(observation["source_comment_ids"])
        require(source <= processed and bool(source & own_targets), "observation_scope_mismatch")
        require(
            set(observation["labels"]) <= set(labels[observation["dimension"]]),
            "invalid_observation_label",
        )
        _quotes(observation["evidence"] + observation["counter_evidence"], visible)
        if observation["assessment_status"] == "assessable":
            require(
                any(e["comment_id"] in own_targets for e in observation["evidence"]),
                "missing_observation_evidence",
            )
        if observation["assessment_status"] in {"insufficient", "not_applicable"}:
            require(
                observation["rationale"].strip()
                or any(s.strip() for s in observation["limitations"]),
                "missing_observation_limitation",
            )
        subject = observation["subject"]
        subject_ids = set(subject["evidence_comment_ids"])
        require(subject_ids <= visible.keys(), "missing_subject_evidence")
        if subject["kind"] != "unknown":
            require(subject_ids, "missing_subject_evidence")
        if subject["target_uid"] is not None:
            require(
                subject["kind"] == "participant"
                and any(
                    visible[cid]["author_uid"] == subject["target_uid"]
                    or visible[cid]["reply_relation"]["target_uid"] == subject["target_uid"]
                    for cid in subject_ids
                ),
                "subject_uid_unproven",
            )
        observations[oid] = observation
    dimensions = value["dimension_results"]
    require(
        Counter((item["uid"], item["dimension"]) for item in dimensions)
        == Counter((uid, dimension) for uid in uids for dimension in DIMENSIONS),
        "invalid_dimension_coverage",
    )
    assigned = []
    for item in dimensions:
        ids = item["observation_ids"]
        require(
            all(
                oid in observations
                and observations[oid]["uid"] == item["uid"]
                and observations[oid]["dimension"] == item["dimension"]
                for oid in ids
            ),
            "invalid_observation_reference",
        )
        assigned.extend(ids)
        incomplete = any(owners[cid] == item["uid"] for cid in remaining)
        limited = any(s.strip() for s in item["limitations"])
        if ids:
            require(
                item["assessment_status"] == aggregate_status([observations[o] for o in ids]),
                "dimension_status_mismatch",
            )
        else:
            require(
                item["assessment_status"] in {"insufficient", "not_applicable"} and limited,
                "missing_dimension_limitation",
            )
            require(
                not incomplete or item["assessment_status"] == "insufficient",
                "dimension_status_mismatch",
            )
        require(not incomplete or limited, "missing_unprocessed_limitation")
    require(Counter(assigned) == Counter(observations.keys()), "orphan_observation")
    prior = {o["observation_id"]: o for o in packet["prior_observations"]}
    require(len(prior) == len(packet["prior_observations"]), "duplicate_prior_observation")
    require(
        packet["task_type"] == "user_synthesis" or packet["phase"] != "primary" or not prior,
        "primary_observations_forbidden",
    )
    require(
        Counter(d["prior_observation_id"] for d in value["prior_dispositions"])
        == Counter(prior.keys()),
        "invalid_prior_partition",
    )
    for item in value["prior_dispositions"]:
        previous = prior[item["prior_observation_id"]]
        require(item["origin_task_id"] == previous["origin_task_id"], "prior_identity_mismatch")
        replacements = item["replacement_observation_ids"]
        require(
            all(
                oid in observations and observations[oid]["uid"] == previous["uid"]
                for oid in replacements
            ),
            "invalid_replacement_observation",
        )
        require(
            item["decision"] not in {"confirmed", "revised"} or replacements,
            "missing_replacement_observation",
        )
        _quotes(item["evidence"], visible)


def validate_result(value, packet, rule_catalog, *, attempt_id, input_sha256) -> None:
    """Check packet-local claims; authorization and delivery remain the acceptor's job."""
    try:
        _validate(value, packet, rule_catalog, attempt_id, input_sha256)
    except OutputError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise OutputError("invalid_result") from None
