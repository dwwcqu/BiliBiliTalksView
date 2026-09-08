from copy import deepcopy

import pytest

from app.analysis_results.contract import DIMENSIONS, schema_document, validate_document
from app.analysis_results.errors import OutputError
from app.analysis_results.validation import validate_result

TASK = "11111111-1111-4111-8111-111111111111"
ATTEMPT = "22222222-2222-4222-8222-222222222222"
SHA = "a" * 64


def baseline():
    packet = {
        "task_id": TASK,
        "run_id": ATTEMPT,
        "video_id": "bilibili:video:1",
        "export_id": TASK,
        "task_type": "user_initial",
        "phase": "primary",
        "synthesis_level": None,
        "scope": {"target_uid": "1", "target_comment_ids": ["10"]},
        "resources": {"analysis_rules": {"sha256": SHA}},
        "prior_observations": [],
        "comments": [
            {
                "comment_id": "10",
                "author_uid": "1",
                "content": {"text": "a test comment"},
                "reply_relation": {"target_uid": None},
            }
        ],
    }
    result = {
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
    }
    result.update(
        protocol="BiliBiliTalksView.AnalysisOutput",
        schema_version="1.0.0",
        artifact_type="task_result",
        attempt_id=ATTEMPT,
        target_uid="1",
        input_sha256=SHA,
        rules_sha256=SHA,
        model_status="completed",
        coverage={"processed_comment_ids": ["10"], "unprocessed_targets": [], "limitations": []},
        observations=[],
        prior_dispositions=[],
        summary="No confident observations.",
        limitations=[],
        dimension_results=[
            {
                "uid": "1",
                "dimension": d,
                "assessment_status": "insufficient",
                "observation_ids": [],
                "limitations": ["Insufficient context."],
            }
            for d in DIMENSIONS
        ],
    )
    return result, packet, {"rules_sha256": SHA, "labels": {d: [] for d in DIMENSIONS}}


def check(result, packet, catalog):
    validate_result(result, packet, catalog, attempt_id=ATTEMPT, input_sha256=SHA)


def test_baseline_and_schema_copy():
    result, packet, catalog = baseline()
    check(result, packet, catalog)
    first = schema_document("task_result")
    first.clear()
    assert schema_document("task_result")["required"]
    validate_document("task_result", result)


@pytest.mark.parametrize(
    "field,value",
    [("attempt_id", TASK), ("input_sha256", "b" * 64), ("phase", "reconcile"), ("target_uid", "2")],
)
def test_identity(field, value):
    result, packet, catalog = baseline()
    check(result, packet, catalog)
    result[field] = value
    with pytest.raises(OutputError):
        check(result, packet, catalog)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_dimension",
        "duplicate_dimension",
        "missing_target",
        "context_target",
        "wrong_status",
        "missing_catalog",
    ],
)
def test_coverage_and_rules(mutation):
    result, packet, catalog = baseline()
    check(result, packet, catalog)
    if mutation == "missing_dimension":
        result["dimension_results"].pop()
    elif mutation == "duplicate_dimension":
        result["dimension_results"].append(deepcopy(result["dimension_results"][0]))
    elif mutation == "missing_target":
        result["coverage"]["processed_comment_ids"] = []
    elif mutation == "context_target":
        result["coverage"]["processed_comment_ids"].append("99")
    elif mutation == "wrong_status":
        result["model_status"] = "unable"
    else:
        catalog["labels"].pop("topic_stance")
    with pytest.raises(OutputError):
        check(result, packet, catalog)


def observation_case():
    result, packet, catalog = baseline()
    observation = {
        "observation_id": TASK + ":o1",
        "origin_task_id": TASK,
        "origin_type": "user_initial",
        "uid": "1",
        "dimension": "topic_stance",
        "subject": {
            "kind": "topic",
            "label": "test",
            "proposition": None,
            "target_uid": None,
            "evidence_comment_ids": ["10"],
        },
        "labels": ["allowed"],
        "assessment_status": "assessable",
        "rationale": "The quote supports this observation.",
        "source_comment_ids": ["10"],
        "evidence": [{"comment_id": "10", "quote": "test"}],
        "counter_evidence": [],
        "limitations": [],
        "export_id": TASK,
        "rules_sha256": SHA,
    }
    result["observations"] = [observation]
    result["dimension_results"][0].update(
        assessment_status="assessable", observation_ids=[TASK + ":o1"]
    )
    catalog["labels"]["topic_stance"] = ["allowed"]
    return result, packet, catalog


@pytest.mark.parametrize(
    "mutation",
    [
        "quote",
        "null_text",
        "context_only",
        "unknown_label",
        "counter_quote",
        "missing_evidence",
        "subject_uid",
        "origin_result",
        "orphan",
        "wrong_priority",
    ],
)
def test_observation_evidence(mutation):
    result, packet, catalog = observation_case()
    check(result, packet, catalog)
    obs = result["observations"][0]
    if mutation == "quote":
        obs["evidence"][0]["quote"] = "fabricated"
    elif mutation == "null_text":
        packet["comments"][0]["content"]["text"] = None
    elif mutation == "context_only":
        packet["comments"].append(
            {
                "comment_id": "11",
                "author_uid": "2",
                "content": {"text": "test"},
                "reply_relation": {"target_uid": None},
            }
        )
        obs["evidence"][0]["comment_id"] = "11"
    elif mutation == "unknown_label":
        obs["labels"] = ["invented"]
    elif mutation == "counter_quote":
        obs["counter_evidence"] = [{"comment_id": "10", "quote": "fabricated"}]
    elif mutation == "missing_evidence":
        obs["evidence"] = []
    elif mutation == "subject_uid":
        obs["subject"].update(kind="participant", target_uid="2")
    elif mutation == "origin_result":
        obs["origin_result"] = {"path": "results/self.json", "sha256": SHA}
    elif mutation == "orphan":
        result["dimension_results"][0].update(observation_ids=[], assessment_status="insufficient")
    else:
        result["dimension_results"][0]["assessment_status"] = "ambiguous"
    with pytest.raises(OutputError):
        check(result, packet, catalog)


@pytest.mark.parametrize("status", ["partial", "unable"])
def test_incomplete_is_valid(status):
    result, packet, catalog = baseline()
    packet["scope"]["target_comment_ids"].append("20")
    packet["comments"].append(dict(packet["comments"][0], comment_id="20"))
    result["model_status"] = status
    unprocessed = ["20"] if status == "partial" else ["10", "20"]
    result["coverage"]["processed_comment_ids"] = ["10"] if status == "partial" else []
    result["coverage"]["unprocessed_targets"] = [
        {"comment_id": cid, "reason": "capacity_exceeded", "detail": "Input limit."}
        for cid in unprocessed
    ]
    check(result, packet, catalog)


def prior_case():
    result, packet, catalog = observation_case()
    result.update(task_type="user_synthesis", synthesis_level=0)
    packet.update(task_type="user_synthesis", synthesis_level=0)
    result["observations"][0]["origin_type"] = "user_synthesis"
    old = deepcopy(result["observations"][0])
    old.update(observation_id=ATTEMPT + ":o1", origin_task_id=ATTEMPT)
    packet["prior_observations"] = [old]
    result["prior_dispositions"] = [
        {
            "prior_observation_id": old["observation_id"],
            "origin_task_id": ATTEMPT,
            "decision": "confirmed",
            "replacement_observation_ids": [TASK + ":o1"],
            "rationale": "Supported within current input.",
            "evidence": [],
        }
    ]
    return result, packet, catalog


@pytest.mark.parametrize("mutation", ["missing", "foreign", "duplicate", "replacement", "uid"])
def test_prior_dispositions(mutation):
    result, packet, catalog = prior_case()
    check(result, packet, catalog)
    if mutation == "missing":
        result["prior_dispositions"] = []
    elif mutation == "foreign":
        result["prior_dispositions"][0]["prior_observation_id"] = TASK + ":o99"
    elif mutation == "duplicate":
        result["prior_dispositions"] *= 2
    elif mutation == "replacement":
        result["prior_dispositions"][0]["replacement_observation_ids"] = []
    else:
        packet["prior_observations"][0]["uid"] = "2"
    with pytest.raises(OutputError):
        check(result, packet, catalog)


def test_unknown_author_thread_needs_no_virtual_dimensions():
    result, packet, catalog = baseline()
    result.update(task_type="thread_context", target_uid=None)
    packet["task_type"] = "thread_context"
    packet["scope"]["target_uid"] = None
    packet["comments"][0]["author_uid"] = None
    result["dimension_results"] = []
    check(result, packet, catalog)
    result["dimension_results"] = baseline()[0]["dimension_results"]
    with pytest.raises(OutputError, match="invalid_dimension_coverage"):
        check(result, packet, catalog)


def test_partial_unprocessed_uid_cannot_be_not_applicable():
    result, packet, catalog = baseline()
    result["model_status"] = "unable"
    result["coverage"]["processed_comment_ids"] = []
    result["coverage"]["unprocessed_targets"] = [
        {"comment_id": "10", "reason": "unable_to_process", "detail": "Not read."}
    ]
    check(result, packet, catalog)
    result["dimension_results"][0]["assessment_status"] = "not_applicable"
    with pytest.raises(OutputError, match="dimension_status_mismatch"):
        check(result, packet, catalog)


def test_catalog_hash_and_missing_catalog_rejected():
    result, packet, catalog = baseline()
    check(result, packet, catalog)
    for bad in (None, {}, dict(catalog, rules_sha256="b" * 64)):
        with pytest.raises(OutputError):
            check(result, packet, bad)
