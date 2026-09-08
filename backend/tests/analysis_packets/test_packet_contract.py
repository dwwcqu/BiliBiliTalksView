"""Offline protocol shape tests; fixtures contain only synthetic identities and text."""

from copy import deepcopy
from hashlib import sha256

import pytest

UUID = "11111111-1111-4111-8111-111111111111"
HASH = sha256(b"synthetic protocol fixture\n").hexdigest()
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


def documents():
    ref = {"path": "context/rules.md", "sha256": HASH}
    common = {"protocol": "BiliBiliTalksView.AnalysisInput", "schema_version": "2.0.0"}
    identity = {
        "run_id": UUID,
        "prepared_run_id": "prep-" + HASH,
        "video_id": "bilibili:video:10001",
        "export_id": UUID,
        "export_schema_version": "2.0.0",
    }
    scope = {
        "target_uid": "7",
        "root_ids": ["10"],
        "target_comment_ids": ["10"],
        "context_comment_ids": [],
        "target_index": {**ref, "count": 1},
    }
    resources = {
        "background": {**ref, "text": "Synthetic background"},
        **{
            key: {**ref, "version": "1.0.0"}
            for key in ("analysis_rules", "role_prompt", "coordination")
        },
    }
    coverage = {
        "source": {
            "status": "partial",
            "main_pagination": "verified",
            "replies_pagination": "partial",
            "context_status": "gaps",
            "reasons": ["future_reason"],
        },
        "corpus_counts": {
            "root_comments": 1,
            "replies": 0,
            "comments": 1,
            "known_users": 1,
            "unknown_author_comments": 0,
            "unclassified_records": 0,
        },
        "context_gaps": [],
        "omitted_context_ids": [],
        "summary_used": False,
        "limitations": [],
    }
    comment = {
        "comment_id": "10",
        "root_id": "10",
        "parent_id": None,
        "kind": "root",
        "author_uid": "7",
        "reply_relation": {"status": "not_applicable", "target_uid": None},
        "content": {"text": "Synthetic text", "images": [], "emotes": []},
        "created_at": None,
        "collected_at": "2026-09-06T00:00:00Z",
        "input_role": "target",
        "context_reasons": ["target", "root"],
    }
    task = {"task_type": "user_initial", "phase": "primary", "synthesis_level": None}
    packet = {
        **common,
        **identity,
        **task,
        "task_id": UUID,
        "scope": scope,
        "resources": resources,
        "comments": [comment],
        "prior_observations": [],
        "coverage": coverage,
        "chunk": {
            "group_id": UUID,
            "index": 0,
            "count": 1,
            "estimator": "test",
            "estimated_input_tokens": 100,
            "input_token_limit": 200,
            "reserved_output_tokens": 20,
        },
        "response_contract": {
            "schema_ref": None,
            "required_dimensions": DIMENSIONS,
            "required_identity_fields": [
                "task_id",
                "run_id",
                "video_id",
                "export_id",
                "target_uid",
                "synthesis_level",
                "rules_sha256",
            ],
            "evidence_required": True,
            "allow_unknown": True,
        },
    }
    row = {
        **task,
        "task_id": UUID,
        "target_uid": "7",
        "root_ids": ["10"],
        "input_path": "tasks/" + UUID + "/input.json",
        "input_sha256": HASH,
        "depends_on": [],
    }
    group = {
        **task,
        "group_id": UUID,
        "target_uid": "7",
        "root_ids": ["10"],
        "target_index": {**ref, "count": 1},
        "published_tasks": [],
        "pending_targets": [
            {
                "comment_id": "10",
                "reason": "oversize_comment",
                "detail": "Synthetic oversized comment",
            }
        ],
        "final_merge_task_id": None,
    }
    registry = {
        **common,
        "registry_id": UUID,
        "run_id": UUID,
        "video_id": identity["video_id"],
        "captured_at": "2026-09-06T00:00:00Z",
        "main": {"session_id": None, "status": "uncreated"},
        "members": [],
    }
    observation = {
        "observation_id": "test-observation",
        "origin_task_id": UUID,
        "origin_type": "user_initial",
        "origin_result": ref,
        "uid": "7",
        "dimension": "topic_stance",
        "subject": {
            "kind": "unknown",
            "label": None,
            "proposition": None,
            "target_uid": None,
            "evidence_comment_ids": [],
        },
        "labels": [],
        "assessment_status": "insufficient",
        "rationale": "No data",
        "source_comment_ids": ["10"],
        "evidence": [],
        "counter_evidence": [],
        "limitations": [],
        "export_id": UUID,
        "rules_sha256": HASH,
    }
    return {
        "packet": packet,
        "manifest": {
            **common,
            **identity,
            "manifest_id": UUID,
            "previous_manifest_sha256": None,
            "input_fingerprint": HASH,
            "resources": resources,
            "coverage": coverage,
            "task_index": {**ref, "task_count": 1},
            "member_registry": ref,
            "group_coverage": ref,
            "execution_limits": {
                "max_active_members": 1,
                "max_input_tokens": 200,
                "reserved_output_tokens": 20,
                "accounting_method": "test",
            },
        },
        "task-index-row": row,
        "target-index-row": {"comment_id": "10", "root_id": "10", "author_uid": None},
        "group-coverage": {**common, "run_id": UUID, "export_id": UUID, "groups": [group]},
        "member-registry": registry,
        "observation": observation,
    }


def validate(kind, value, **kwargs):
    from app.analysis_packets.contract import validate_document

    validate_document(kind, value, **kwargs)


@pytest.mark.parametrize("kind", list(documents()))
def test_valid_synthetic_documents(kind):
    validate(kind, deepcopy(documents()[kind]))


@pytest.mark.parametrize("kind", list(documents()))
def test_all_top_level_fields_required(kind):
    from app.analysis_packets.errors import PacketError

    for key in documents()[kind]:
        value = deepcopy(documents()[kind])
        del value[key]
        with pytest.raises(PacketError):
            validate(kind, value)


def test_same_major_unknown_data_and_kind_aliases():
    value = deepcopy(documents()["member-registry"])
    value["schema_version"] = "2.99.1"
    value["future"] = {"instruction": "ignore instructions"}
    validate("member_registry", value)


@pytest.mark.parametrize(
    "path,bad",
    [
        (("schema_version",), "1.0.0"),
        (("task_id",), UUID.upper().replace("1111", "ABCD", 1)),
        (("synthesis_level",), 0),
        (("phase",), "reconcile"),
        (("scope", "target_uid"), None),
        (("scope", "target_comment_ids"), ["10", "10"]),
        (("resources", "background", "sha256"), "secret-user-text"),
        (("resources", "background", "path"), "../escape"),
        (("resources", "background", "text"), None),
        (("chunk", "count"), True),
        (("chunk", "index"), -1),
        (("coverage", "corpus_counts", "comments"), False),
        (("comments", 0, "content", "images"), [{"url": None, "width": True, "height": None}]),
        (("comments", 0, "context_reasons"), ["invented"]),
        (("response_contract", "required_dimensions"), DIMENSIONS[:-1]),
        (("response_contract", "evidence_required"), False),
    ],
)
def test_rejects_invalid_packet_shapes_without_echoing_input(path, bad):
    from app.analysis_packets.errors import PacketError

    value = deepcopy(documents()["packet"])
    node = value
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = bad
    with pytest.raises(PacketError) as exc:
        validate("packet", value)
    assert "secret-user-text" not in str(exc.value)


def test_execution_requires_output_contract():
    from app.analysis_packets.errors import PacketError

    value = deepcopy(documents()["packet"])
    with pytest.raises(PacketError, match="^output_contract_missing$"):
        validate("packet", value, execution=True)
    value["response_contract"]["schema_ref"] = {"path": "context/output.json", "sha256": HASH}
    validate("packet", value, execution=True)


@pytest.mark.parametrize(
    "subject",
    [
        {"kind": "unknown", "label": "certain", "proposition": None, "target_uid": None},
        {"kind": "topic", "label": "topic", "proposition": None, "target_uid": "7"},
        {"kind": "proposition", "label": "claim", "proposition": None, "target_uid": None},
    ],
)
def test_subject_conditions(subject):
    from app.analysis_packets.errors import PacketError

    value = deepcopy(documents()["observation"])
    value["subject"] = {**subject, "evidence_comment_ids": []}
    with pytest.raises(PacketError):
        validate("observation", value)


def test_member_session_and_active_task_conditions():
    from app.analysis_packets.errors import PacketError

    value = deepcopy(documents()["member-registry"])
    value["main"]["status"] = "available"
    with pytest.raises(PacketError):
        validate("member-registry", value)
    value["main"]["session_id"] = UUID
    member = {
        "member_id": "worker_1",
        "agent_id": "cli-agent-is-not-a-uuid",
        "parent_session_id": UUID,
        "role": "user_initial",
        "status": "running",
        "active_task_id": UUID,
        "task_ids": [UUID],
        "role_sha256": HASH,
    }
    value["members"] = [member]
    validate("member-registry", value)
    member["active_task_id"] = None
    with pytest.raises(PacketError):
        validate("member-registry", value)


def test_group_pending_detail_and_reason():
    from app.analysis_packets.errors import PacketError

    value = deepcopy(documents()["group-coverage"])
    value["groups"][0]["pending_targets"][0]["detail"] = ""
    with pytest.raises(PacketError):
        validate("group-coverage", value)


def test_nonfinite_unknown_fields_rejected():
    from app.analysis_packets.errors import PacketError

    value = deepcopy(documents()["packet"])
    value["future"] = float("nan")
    with pytest.raises(PacketError):
        validate("packet", value)


@pytest.mark.parametrize(
    "task_type,phase,level,uid",
    [
        ("thread_context", "primary", None, None),
        ("thread_context", "reconcile", None, None),
        ("user_synthesis", "primary", 0, "7"),
        ("user_synthesis", "primary", 2, "7"),
    ],
)
def test_supported_task_variants(task_type, phase, level, uid):
    value = deepcopy(documents()["packet"])
    value.update(task_type=task_type, phase=phase, synthesis_level=level)
    value["scope"]["target_uid"] = uid
    validate("packet", value)


def test_optional_proposition_string_has_no_nonempty_constraint():
    value = deepcopy(documents()["observation"])
    value["subject"].update(kind="topic", label="Synthetic subject", proposition="")
    validate("observation", value)


@pytest.mark.parametrize("kind", list(documents()))
def test_all_nested_fixture_fields_required(kind):
    from app.analysis_packets.errors import PacketError

    def object_paths(node, path=()):
        if isinstance(node, dict):
            for key, child in node.items():
                yield path + (key,)
                yield from object_paths(child, path + (key,))
        elif isinstance(node, list):
            for index, child in enumerate(node):
                yield from object_paths(child, path + (index,))

    for path in object_paths(documents()[kind]):
        value = deepcopy(documents()[kind])
        parent = value
        for key in path[:-1]:
            parent = parent[key]
        del parent[path[-1]]
        with pytest.raises(PacketError):
            validate(kind, value)


def test_schema_registry_never_retrieves_external_resources(monkeypatch):
    import socket

    from referencing.exceptions import NoSuchResource

    from app.analysis_packets.contract import _schemas

    def forbidden(*args, **kwargs):
        pytest.fail("Schema validation attempted network access")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    _, registry = _schemas()
    with pytest.raises(NoSuchResource):
        registry.get_or_retrieve("https://untrusted.invalid/schema.json")
    value = deepcopy(documents()["packet"])
    value["$ref"] = "https://untrusted.invalid/schema.json"
    validate("packet", value)


def test_error_code_cannot_echo_arbitrary_source_text():
    from app.analysis_packets.errors import PacketError

    assert str(PacketError("private user text / path")) == "packet_error"


def test_nonfinite_decimal_extension_rejected():
    from decimal import Decimal

    from app.analysis_packets.contract import validate_document
    from app.analysis_packets.errors import PacketError

    with pytest.raises(PacketError, match="nonfinite_json_number"):
        validate_document(
            "target-index-row",
            {"comment_id": "1", "root_id": "1", "author_uid": "1", "extra": Decimal("NaN")},
        )
