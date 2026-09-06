import pytest
from sqlalchemy import func, select

from app.comment_export.export import build_batch
from app.storage.errors import StorageError
from app.storage.frozen import freeze_batch
from app.storage.importer import import_batch
from app.storage.schema import comments, discussion_states


def test_repeated_import_is_idempotent(conn, frozen_case, tmp_path):
    path = build_batch(*frozen_case, tmp_path / "batch")
    with freeze_batch(path, tmp_path / "work") as frozen:
        first = import_batch(conn, frozen)
        second = import_batch(conn, frozen)
    assert first["state_id"] == second["state_id"]
    assert second["receipt_status"] == "ready"
    assert conn.scalar(select(func.count()).select_from(comments)) == 3
    assert conn.scalar(select(func.count()).select_from(discussion_states)) == 1


def test_same_export_id_cannot_change_content(conn, frozen_case, tmp_path):
    records, meta = frozen_case
    first = build_batch(records, meta, tmp_path / "first")
    with freeze_batch(first, tmp_path / "work") as frozen:
        import_batch(conn, frozen)
    records[0]["content"]["text"] = "变化"
    second = build_batch(records, meta, tmp_path / "second")
    with (
        freeze_batch(second, tmp_path / "work") as frozen,
        pytest.raises(StorageError, match="export_id_conflict"),
    ):
        import_batch(conn, frozen)


def test_same_time_identical_reexport_can_publish(conn, frozen_case, tmp_path):
    from copy import deepcopy
    from uuid import uuid4

    records, metadata = frozen_case
    first = build_batch(records, metadata, tmp_path / "first")
    with freeze_batch(first, tmp_path / "work") as frozen:
        original = import_batch(conn, frozen, publish=True)
    second_meta = deepcopy(metadata)
    second_meta["export_id"] = str(uuid4())
    second_rows = [dict(row, export_id=second_meta["export_id"]) for row in records]
    second = build_batch(second_rows, second_meta, tmp_path / "second")
    with freeze_batch(second, tmp_path / "work") as frozen:
        result = import_batch(conn, frozen, publish=True)
    assert result["published"]
    assert result["state_id"] != original["state_id"]


def test_inconsistent_failed_receipt_cannot_overwrite_current(conn, frozen_case, tmp_path):
    from sqlalchemy import update

    from app.storage.schema import import_receipts

    path = build_batch(*frozen_case, tmp_path / "batch")
    with freeze_batch(path, tmp_path / "work") as frozen:
        import_batch(conn, frozen, publish=True)
        with conn.begin():
            conn.execute(update(import_receipts).values(status="failed"))
        with pytest.raises(StorageError, match="inconsistent_import_state"):
            import_batch(conn, frozen)


def test_v2_directory_migration_preserves_same_observation(conn, frozen_case, tmp_path):
    from copy import deepcopy
    from uuid import uuid4

    records, metadata = frozen_case
    first = build_batch(records, metadata, tmp_path / "v1")
    with freeze_batch(first, tmp_path / "work") as frozen:
        import_batch(conn, frozen, publish=True)
    migrated = deepcopy(metadata)
    migrated.update(schema_version="2.0.0", export_id=str(uuid4()))
    rows = [dict(row, schema_version="2.0.0", export_id=migrated["export_id"]) for row in records]
    second = build_batch(rows, migrated, tmp_path / "v2")
    with freeze_batch(second, tmp_path / "work") as frozen:
        result = import_batch(conn, frozen, publish=True)
    assert result["published"]
