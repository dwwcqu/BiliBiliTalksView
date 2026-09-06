from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.comment_export.export import build_batch
from app.storage.errors import StorageError
from app.storage.frozen import freeze_batch
from app.storage.importer import import_batch
from app.storage.schema import comments, discussion_states, import_receipts, videos


def batch(frozen_case, tmp_path, name, *, partial=False):
    rows, meta = deepcopy(frozen_case)
    meta["export_id"] = str(uuid4())
    meta["captured_to"] = "2026-09-05T09:00:01Z" if name != "first" else "2026-09-05T09:00:00Z"
    if partial:
        meta["coverage"]["status"] = "partial"
        meta["coverage"]["main_pagination"] = "partial"
    for row in rows:
        row["export_id"] = meta["export_id"]
    return build_batch(rows, meta, tmp_path / name)


def test_partial_does_not_replace_and_new_current_cleans_old(conn, frozen_case, tmp_path):
    first = batch(frozen_case, tmp_path, "first")
    with freeze_batch(first, tmp_path / "work") as frozen:
        original = import_batch(conn, frozen, publish=True)
    partial = batch(frozen_case, tmp_path, "partial", partial=True)
    with freeze_batch(partial, tmp_path / "work") as frozen:
        incomplete = import_batch(conn, frozen, publish=True)
    assert not incomplete["published"]
    with conn.begin():
        assert conn.scalar(select(videos.c.current_state_id)) == original["state_id"]
    newer = batch(frozen_case, tmp_path, "new")
    # A later observation can replace the partial working state.
    import json

    p = newer / "manifest.json"
    m = json.loads(p.read_text(encoding="utf-8"))
    m["captured_to"] = "2026-09-05T09:00:02Z"
    p.write_text(json.dumps(m), encoding="utf-8")
    with freeze_batch(newer, tmp_path / "work") as frozen:
        current = import_batch(conn, frozen, publish=True)
    assert current["published"]
    with conn.begin():
        assert conn.scalar(select(func.count()).select_from(discussion_states)) == 1
        assert conn.scalar(select(func.count()).select_from(comments)) == 3
        assert (
            conn.scalar(
                select(func.count())
                .select_from(import_receipts)
                .where(import_receipts.c.status == "expired")
            )
            == 2
        )
    with (
        freeze_batch(first, tmp_path / "work") as frozen,
        pytest.raises(StorageError, match="already_imported_expired"),
    ):
        import_batch(conn, frozen)


def test_publish_returns_decoded_coverage_extensions(conn, frozen_case, tmp_path):
    records, metadata = frozen_case
    metadata["coverage"]["extension"] = "注释\0字面\\0"
    source = build_batch(records, metadata, tmp_path / "batch")
    with freeze_batch(source, tmp_path / "work") as frozen:
        result = import_batch(conn, frozen, publish=True)
    assert result["coverage"]["extension"] == metadata["coverage"]["extension"]
