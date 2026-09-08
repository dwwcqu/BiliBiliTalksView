"""Invented output fixtures; trusted execution evidence is simulated explicitly."""

from copy import deepcopy
from uuid import uuid4

import pytest

from app.analysis_input.storage import prepare_input
from app.analysis_packets.budget import Budget
from app.analysis_packets.builder import DIMENSIONS, build_primary, prepare_resources
from app.analysis_packets.publication import publish_assembly
from app.analysis_packets.source import load_source
from app.comment_export.export import build_batch, write_json


@pytest.fixture
def output_case(frozen_case, tmp_path):
    from app.analysis_results.acceptance import bind_output_schema

    rows, meta = deepcopy(frozen_case)
    meta["export_id"] = str(uuid4())
    meta["schema_version"] = "2.0.0"
    for row in rows:
        row.update(export_id=meta["export_id"], schema_version="2.0.0")
    container = tmp_path / "source"
    build_batch(rows, meta, container / "batches" / meta["export_id"])
    write_json(
        container / "current.json",
        {k: meta[k] for k in ("schema_version", "export_id", "video_id")}
        | {"batch_path": "batches/" + meta["export_id"]},
    )
    context = {}
    for name in ("rules.md", "role.md", "coord.md"):
        file = tmp_path / name
        file.write_text("合成规则", encoding="utf-8")
        context[name] = file
    root = tmp_path / "analysis"
    prepared = prepare_input(container, root, context_files=context)
    bundle = load_source(root, prepared["analysis_run_id"], meta["video_id"])
    resources, files = prepare_resources(
        bundle,
        {
            "analysis_rules": {"name": "rules.md", "version": "test"},
            "role_prompt": {"name": "role.md", "version": "test"},
            "coordination": {"name": "coord.md", "version": "test"},
        },
    )
    budget = Budget(100000, 1000, 120000, 2)
    assembly = bind_output_schema(
        build_primary(bundle, str(uuid4()), resources, budget, resource_files=files)
    )
    from result_examples import register_members

    register_members(assembly)
    publication = publish_assembly(root, assembly, bundle)
    rules = {
        "rules_sha256": resources["analysis_rules"]["sha256"],
        "labels": {d: ["测试标签"] for d in DIMENSIONS},
    }
    return {
        "root": root,
        "bundle": bundle,
        "assembly": assembly,
        "budget": budget,
        "publication": publication,
        "rules": rules,
    }
