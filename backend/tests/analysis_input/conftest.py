"""Only invented data; publication never invokes network or models."""
from copy import deepcopy
from uuid import uuid4

import pytest

from app.comment_export.export import build_batch, write_json


@pytest.fixture
def make_export(frozen_case, tmp_path):
    def make(*, version="2.0.0", partial=False, container=None, export_id=None):
        records, metadata = deepcopy(frozen_case)
        metadata.update(schema_version=version, export_id=export_id or str(uuid4()))
        if partial:
            metadata["coverage"].update(status="partial", main_pagination="partial")
            metadata["coverage"]["reasons"] = ["main_incomplete"]
        for row in records:
            row.update(schema_version=version, export_id=metadata["export_id"])
        container = container or tmp_path / ("source-" + metadata["export_id"])
        batch = build_batch(records, metadata, container / "batches" / metadata["export_id"])
        (batch / "README.md").write_text("合成视频背景", encoding="utf-8")
        pointer = {key: metadata[key] for key in ("schema_version", "video_id", "export_id")}
        pointer["batch_path"] = "batches/" + metadata["export_id"]
        write_json(container / "current.json", pointer)
        return container, batch
    return make
