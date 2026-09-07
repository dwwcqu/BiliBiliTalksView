"""Compare file and direct materialization in independently migrated schemas."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.pool import NullPool
from test_handoff import evidence_case

from app.comment_export.dataset import ValidatedDataset
from app.comment_export.dataset_builder import build_dataset
from app.comment_export.export import write_dataset
from app.comment_export.validation import load_validated_documents
from app.storage import exporter
from app.storage.codec import decode_json
from app.storage.frozen import canonical_digest, freeze_batch
from app.storage.handoff import materialize, prepare_dataset_handoff, prepare_handoff
from app.storage.schema import discussion_states, import_receipts, videos
from app.storage.semantic import from_frozen, from_state


@pytest.fixture
def parity_engine(db_engine, migration_config):
    # db_engine has already verified the dedicated test database. A second schema
    # prevents receipt reuse from making the second adapter skip actual insertion.
    schema = "bt_test_" + uuid4().hex
    with db_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        db_engine.url, poolclass=NullPool, hide_parameters=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        config = Config(migration_config.config_file_name)
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()
        # This name is generated above, never supplied through an input path.
        with db_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def test_file_and_direct_materialize_and_reexport_identically(
    db_engine, parity_engine, frozen_case, tmp_path, monkeypatch
):
    rows, metadata = evidence_case(frozen_case)
    documents = dict(build_dataset(rows, metadata).iter_documents())
    documents["manifest.json"]["extension"] = {"fraction": 1.0, "nested": ["source"]}
    for entry in documents["manifest.json"]["threads"]:
        documents[entry["path"]]["source_title"] = "preserved source title"
        documents[entry["path"]]["extension"] = {"number": 1.0}
    for entry in documents["manifest.json"]["users"]:
        documents[entry["path"]]["extension"] = {"number": 1.0}
    dataset = ValidatedDataset.from_documents(documents, evidence=metadata)
    source = write_dataset(dataset, tmp_path / "source")
    expected = from_frozen(dataset)
    job_id = "e5b77d23-5088-44e4-9bb1-f62a353da6d7"

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 8, 0, 0, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(exporter, "datetime", FixedDatetime)
    monkeypatch.setattr(exporter, "uuid4", lambda: UUID("6d0818a3-688a-4702-8112-1ba35b50d1ed"))
    observations, contexts, receipt_values, exports, export_digests = [], [], [], [], []
    state_ids, cache_versions = [], []
    with freeze_batch(source, tmp_path / "freeze") as frozen:
        assert frozen.digest == dataset.digest == canonical_digest(source)
        for engine, adapter, route in (
            (db_engine, frozen, "file"), (parity_engine, dataset, "direct")
        ):
            handoff = (prepare_handoff(adapter, rows, metadata, job_id, 0)
                       if route == "file" else prepare_dataset_handoff(adapter, job_id, 0))
            with engine.connect() as connection:
                result = materialize(connection, adapter, handoff, lambda *_: None)
                assert result["published"]
                state_ids.append(result["state_id"])
                with connection.begin():
                    state = connection.execute(select(discussion_states)).mappings().one()
                    observations.append(from_state(connection, state))
                    contexts.append(decode_json(state["refresh_context"]))
                    receipt = connection.execute(select(import_receipts)).mappings().one()
                    receipt_values.append((receipt["source_export_id"],
                                           receipt["canonical_digest"], receipt["status"]))
                    video = connection.execute(select(videos)).mappings().one()
                    assert video["current_state_id"] == result["state_id"]
                    cache_versions.append(video["cache_version"])
                exported = exporter.export_state(connection, metadata["video_id"],
                                                  tmp_path / route)
                exports.append(load_validated_documents(exported))
                export_digests.append(canonical_digest(exported))

    assert state_ids[0] != state_ids[1]  # Both adapters performed a fresh materialization.
    assert cache_versions[0] == cache_versions[1] > 0
    assert observations[0] == observations[1] == expected
    assert contexts[0] == contexts[1]
    assert receipt_values[0] == receipt_values[1]
    assert exports[0] == exports[1]
    assert export_digests[0] == export_digests[1]
    assert type(exports[0]["manifest.json"]["extension"]["fraction"]) is float
