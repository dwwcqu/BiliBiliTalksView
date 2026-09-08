"""Schema tests use invented values and never access a service."""

from copy import deepcopy
from decimal import Decimal

import pytest
from test_output_validation import ATTEMPT, SHA, TASK, baseline, observation_case

from app.analysis_packets.codec import json_bytes, loads
from app.analysis_results.contract import DIMENSIONS, validate_document
from app.analysis_results.errors import OutputError


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "2.0.0"),
        ("attempt_id", TASK.upper()),
        ("target_uid", True),
        ("rules_sha256", "A" * 64),
        ("synthesis_level", True),
    ],
)
def test_bad_scalars(field, value):
    result, _, _ = baseline()
    validate_document("task_result", result)
    # The numeric-only UUID fixture needs a genuinely uppercase hex character.
    if field == "attempt_id":
        value = "AAAAAAAA-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    result[field] = value
    with pytest.raises(OutputError):
        validate_document("task_result", result)


def test_extensions_preserve_exact_numbers_and_reject_nonfinite():
    result, _, _ = baseline()
    result["future_metadata"] = {"score": Decimal("0.12345678901234567890123456789")}
    validate_document("task_result", result)
    assert loads(json_bytes(result).decode()) == result
    result["future_metadata"]["score"] = Decimal("NaN")
    with pytest.raises(OutputError, match="nonfinite_json_number"):
        validate_document("task_result", result)


def test_missing_nested_field_and_forbidden_self_reference():
    result, _, _ = observation_case()
    validate_document("task_result", result)
    missing = deepcopy(result)
    del missing["observations"][0]["subject"]["target_uid"]
    with pytest.raises(OutputError):
        validate_document("task_result", missing)
    result["observations"][0]["origin_result"] = {"path": "results/self.json", "sha256": SHA}
    with pytest.raises(OutputError):
        validate_document("task_result", result)


def test_receipt_time_and_reference():
    receipt = {
        "protocol": "BiliBiliTalksView.AnalysisOutput",
        "schema_version": "1.0.0",
        "artifact_type": "validation_receipt",
        "task_id": TASK,
        "run_id": TASK,
        "attempt_id": ATTEMPT,
        "input_sha256": SHA,
        "decision": "accepted",
        "reason_codes": [],
        "validated_at": "2026-09-06T12:00:00Z",
        "result_ref": {"path": "results/a.json", "sha256": SHA},
    }
    validate_document("validation_receipt", receipt)
    for field, value in [
        ("validated_at", "2026-02-30T12:00:00Z"),
        ("validated_at", "2026-09-06T12:00:00+00:00"),
        ("result_ref", None),
        ("result_ref", {"path": "../escape.json", "sha256": SHA}),
    ]:
        bad = dict(receipt, **{field: value})
        with pytest.raises(OutputError):
            validate_document("validation_receipt", bad)


def test_profile_required_structure_and_order():
    ref = {"path": "context/rules.md", "sha256": SHA}
    profile = {
        "protocol": "BiliBiliTalksView.AnalysisOutput",
        "schema_version": "1.0.0",
        "artifact_type": "profile_candidate",
        "video_id": "bilibili:video:1",
        "uid": "1",
        "display_nickname": None,
        "video_title": None,
        "run_id": TASK,
        "prepared_run_id": "prep-" + SHA,
        "export_id": TASK,
        "export_schema_version": "2.0.0",
        "input_fingerprint": SHA,
        "created_at": "2026-09-06T12:00:00Z",
        "context": {
            k: dict(ref, version="1")
            for k in ("background", "analysis_rules", "role_prompt", "coordination")
        }
        | {"execution_ref": ref},
        "source_coverage": {
            "status": "partial",
            "main_pagination": "partial",
            "replies_pagination": "partial",
            "context_status": "gaps",
            "reasons": ["missing"],
        },
        "corpus_counts": {
            k: 0
            for k in (
                "root_comments",
                "replies",
                "comments",
                "known_users",
                "unknown_author_comments",
                "unclassified_records",
            )
        },
        "analysis_coverage": {
            "status": "partial",
            "target_comment_ids": ["10"],
            "processed_comment_ids": [],
            "unprocessed_targets": [
                {"comment_id": "10", "reason": "dependency_unavailable", "detail": "Pending."}
            ],
            "required_task_ids": [TASK],
            "accepted_task_ids": [],
            "missing_task_ids": [TASK],
        },
        "summary": "Pending.",
        "dimensions": [
            {
                "dimension": d,
                "assessment_status": "insufficient",
                "observations": [],
                "limitations": ["Pending."],
            }
            for d in DIMENSIONS
        ],
        "source_results": [],
        "limitations": ["Pending."],
    }
    validate_document("user_profile", profile)
    validate_document("profile_candidate", profile)
    bad = deepcopy(profile)
    bad["dimensions"].reverse()
    with pytest.raises(OutputError):
        validate_document("user_profile", bad)
    bad = deepcopy(profile)
    bad["artifact_type"] = "user_profile"
    with pytest.raises(OutputError):
        validate_document("user_profile", bad)
