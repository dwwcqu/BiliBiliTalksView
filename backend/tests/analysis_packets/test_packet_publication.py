import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.analysis_packets.budget import Budget
from app.analysis_packets.builder import build_primary, prepare_resources
from app.analysis_packets.publication import publish_assembly, validate_published
from app.analysis_packets.source import load_source


def test_publish_and_validate_offline(prepared_case, resource_config):
    root, record = prepared_case()
    source = load_source(root, record["analysis_run_id"], record["video_id"])
    resources, files = prepare_resources(source, resource_config)
    assembly = build_primary(
        source, str(uuid4()), resources, Budget(100000, 1000, 110000, 2), resource_files=files
    )
    result = publish_assembly(root, assembly, source)
    assert result["status"] == "offline_prepared"
    assert result["model_execution_authorized"] is False
    checked = validate_published(root, result["run_id"], result["manifest_id"])
    assert checked["task_count"] == len(assembly.packets)
    assert list((Path(result["run_path"]) / "users").iterdir()) == []
    task = next((Path(result["run_path"]) / "tasks").glob("*/input.json"))
    value = json.loads(task.read_text(encoding="utf-8"))
    value["scope"]["target_comment_ids"] = []
    task.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        validate_published(root, result["run_id"], result["manifest_id"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("export_id", "11111111-1111-4111-8111-111111111111"),
        ("export_schema_version", "1.0.0"),
        ("previous_manifest_sha256", "0" * 64),
    ],
)
def test_manifest_source_and_history_tampering_rejected(
    prepared_case, resource_config, field, value
):
    root, record = prepared_case()
    source = load_source(root, record["analysis_run_id"], record["video_id"])
    resources, files = prepare_resources(source, resource_config)
    assembly = build_primary(
        source, str(uuid4()), resources, Budget(100000, 1000, 110000, 2), resource_files=files
    )
    result = publish_assembly(root, assembly, source)
    path = Path(result["manifest_path"])
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest[field] = value
    path.write_bytes((json.dumps(manifest) + "\n").encode())
    with pytest.raises(ValueError):
        validate_published(root, result["run_id"], result["manifest_id"])


def test_extra_resource_cannot_be_published_as_profile(prepared_case, resource_config):
    root, record = prepared_case()
    source = load_source(root, record["analysis_run_id"], record["video_id"])
    resources, files = prepare_resources(source, resource_config)
    assembly = build_primary(
        source, str(uuid4()), resources, Budget(100000, 1000, 110000, 2), resource_files=files
    )
    result = publish_assembly(root, assembly, source)
    assembly.previous_manifest_sha256 = result["manifest_sha256"]
    assembly.resource_files["users/10.json"] = b'{"fake":"profile"}'
    with pytest.raises(ValueError):
        publish_assembly(root, assembly, source)
    assert not (Path(result["run_path"]) / "users/10.json").exists()


def test_disk_accepted_result_tampering_is_rejected(prepared_case, resource_config):
    from test_packet_advanced import accept
    from test_packet_semantics import make_primary

    from app.analysis_packets.advanced import build_reconcile

    bundle, primary, budget = make_primary(prepared_case, resource_config)
    accepted = {p["task_id"]: accept(p) for p in primary.packets}
    advanced = build_reconcile(bundle, primary, accepted, budget)
    # prepared_case created its analysis root inside the private tmp_path.
    root, _ = prepared_case()
    result = publish_assembly(root, advanced, bundle, accepted)
    used = next(dep for row in advanced.task_rows for dep in row["depends_on"])
    (Path(result["run_path"]) / accepted[used].result_path).write_bytes(b"tampered")
    with pytest.raises(ValueError):
        validate_published(root, result["run_id"], result["manifest_id"], accepted)


def test_catalog_only_dependency_cannot_be_published(prepared_case, resource_config):
    from test_packet_advanced import accept, observation
    from test_packet_semantics import make_primary

    from app.analysis_packets.advanced import build_reconcile

    bundle, primary, budget = make_primary(prepared_case, resource_config)
    accepted = {p["task_id"]: accept(p) for p in primary.packets}
    initial = next(
        p
        for p in primary.packets
        if p["task_type"] == "user_initial" and p["scope"]["target_uid"] == "10"
    )
    accepted[initial["task_id"]] = accept(initial, [observation(initial, bundle)])
    removed = {p["task_id"] for p in primary.packets if p["task_type"] == "user_initial"}
    primary.packets = [p for p in primary.packets if p["task_id"] not in removed]
    primary.task_rows = [r for r in primary.task_rows if r["task_id"] not in removed]
    primary.group_coverage["groups"] = [
        g for g in primary.group_coverage["groups"] if g["task_type"] != "user_initial"
    ]
    advanced = build_reconcile(bundle, primary, accepted, budget)
    root, _ = prepared_case()
    with pytest.raises(ValueError):
        publish_assembly(root, advanced, bundle, accepted)


def test_windows_transient_directory_lock_retries(prepared_case, resource_config, monkeypatch):
    root, record = prepared_case()
    source = load_source(root, record["analysis_run_id"], record["video_id"])
    resources, files = prepare_resources(source, resource_config)
    assembly = build_primary(
        source, str(uuid4()), resources, Budget(100000, 1000, 110000, 2), resource_files=files
    )
    actual = Path.rename
    attempts = []

    def flaky(self, target):
        if self.name == "run":
            attempts.append(1)
            if len(attempts) == 1:
                error = PermissionError("temporary scanner lock")
                error.winerror = 5
                raise error
        return actual(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    result = publish_assembly(root, assembly, source)
    assert result["status"] == "offline_prepared"
    assert len(attempts) == 2
