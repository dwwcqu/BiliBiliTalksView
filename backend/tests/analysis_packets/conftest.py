from copy import deepcopy
from uuid import uuid4

import pytest

from app.analysis_input.storage import prepare_input
from app.comment_export.export import build_batch, write_json


@pytest.fixture
def prepared_case(frozen_case, tmp_path):
    def make(version="2.0.0", *, overlong=False):
        rows, metadata = deepcopy(frozen_case)
        metadata.update(schema_version=version, export_id=str(uuid4()))
        rows = [dict(row, schema_version=version, export_id=metadata["export_id"]) for row in rows]
        for row in rows:
            if row["author"]["uid"]:
                row["author"]["uid"] = "10"
        if overlong:
            next(row for row in rows if row["comment_id"] == "200")["content"]["text"] = "x" * 30000
        extra = deepcopy(rows[0])
        extra.update(comment_id="102", parent_id="100")
        extra["author"] = {"uid": "20", "nickname": "other"}
        extra["content"]["text"] = "请补充理由。"
        rows.append(extra)
        source = tmp_path / ("source-" + metadata["export_id"])
        batch = build_batch(rows, metadata, source / "batches" / metadata["export_id"])
        (batch / "README.md").write_text("虚构背景", encoding="utf-8")
        write_json(
            source / "current.json",
            {k: metadata[k] for k in ("schema_version", "video_id", "export_id")}
            | {"batch_path": "batches/" + metadata["export_id"]},
        )
        context = {}
        for name in ("analysis-standard.md", "role.md", "coordination.md"):
            path = tmp_path / name
            path.write_text("合成规则", encoding="utf-8")
            context[name] = path
        root = tmp_path / "analysis"
        result = prepare_input(source, root, context_files=context)
        return root, result

    return make


@pytest.fixture
def resource_config():
    return {
        "analysis_rules": {"name": "analysis-standard.md", "version": "0.1"},
        "role_prompt": {"name": "role.md", "version": "0.1"},
        "coordination": {"name": "coordination.md", "version": "0.1"},
    }
