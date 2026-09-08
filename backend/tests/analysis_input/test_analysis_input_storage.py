import json
from pathlib import Path

import pytest

from app.analysis_input.errors import InputPreparationError
from app.analysis_input.storage import prepare_input


def test_reuse_and_background_version(make_export, tmp_path):
    source, batch = make_export()
    root = tmp_path / "analysis"
    first = prepare_input(source, root)
    assert first["status"] == "ready"
    assert prepare_input(source, root)["analysis_run_id"] == first["analysis_run_id"]
    (batch / "README.md").write_text("更新背景", encoding="utf-8")
    second = prepare_input(source, root)
    assert first["analysis_run_id"] != second["analysis_run_id"]
    assert first["input_path"] == second["input_path"]
    assert (Path(first["run_path"]) / "context/README.md").read_text(encoding="utf-8") == "合成视频背景"
    assert (Path(second["run_path"]) / "context/README.md").read_text(encoding="utf-8") == "更新背景"
    assert list((Path(first["run_path"]) / "users").iterdir()) == []


def test_partial_needs_explicit_policy(make_export, tmp_path):
    source, _ = make_export(partial=True)
    result = prepare_input(source, tmp_path / "analysis")
    assert result["status"] == "waiting_policy"
    allowed = prepare_input(source, tmp_path / "analysis", allow_partial=True)
    assert allowed["status"] == "ready"
    assert result["analysis_run_id"] != allowed["analysis_run_id"]


@pytest.mark.parametrize("kind", ["same", "child", "parent"])
def test_output_cannot_overlap_source(make_export, tmp_path, kind):
    source, _ = make_export()
    target = {"same": source, "child": source / "analysis", "parent": tmp_path}[kind]
    with pytest.raises(InputPreparationError, match="invalid_storage_root"):
        prepare_input(source, target)
    assert not (source / "analysis").exists()


@pytest.mark.parametrize("name", ["readme.md", "README.MD", "../x", "a/b", "CON", "x.", "x "])
def test_context_names_rejected(make_export, tmp_path, name):
    source, batch = make_export()
    with pytest.raises(InputPreparationError, match="invalid_context"):
        prepare_input(source, tmp_path / "analysis", context_files={name: batch / "README.md"})


def test_casefold_context_collision(make_export, tmp_path):
    source, batch = make_export()
    with pytest.raises(InputPreparationError, match="invalid_context"):
        prepare_input(source, tmp_path / "analysis",
                      context_files={"A.md": batch / "README.md", "a.md": batch / "README.md"})


def test_stored_context_corruption_rejected(make_export, tmp_path):
    source, _ = make_export()
    root = tmp_path / "analysis"
    result = prepare_input(source, root)
    (Path(result["run_path"]) / "context/README.md").write_text("tampered")
    with pytest.raises(InputPreparationError, match="stored_run_invalid"):
        prepare_input(source, root)


def test_stored_input_corruption_rejected(make_export, tmp_path):
    source, _ = make_export()
    root = tmp_path / "analysis"
    result = prepare_input(source, root)
    (Path(result["input_path"]) / "manifest.json").write_text("{}")
    with pytest.raises(InputPreparationError, match="stored_input_invalid"):
        prepare_input(source, root)


def test_json_key_order_does_not_create_conflict(make_export, tmp_path):
    source, batch = make_export()
    root = tmp_path / "analysis"
    first = prepare_input(source, root)
    path = batch / "manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    assert prepare_input(source, root)["analysis_run_id"] == first["analysis_run_id"]


def test_precise_extension_number_change_is_conflict(make_export, tmp_path):
    source, batch = make_export()
    path = batch / "manifest.json"
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace('{', '{"extension":0.123456789012345678901,', 1),
                    encoding="utf-8", newline="\n")
    root = tmp_path / "analysis"
    prepare_input(source, root)
    path.write_text(original.replace('{', '{"extension":0.123456789012345678902,', 1),
                    encoding="utf-8", newline="\n")
    with pytest.raises(InputPreparationError, match="input_conflict"):
        prepare_input(source, root)


def test_run_publish_failure_can_retry(make_export, tmp_path, monkeypatch):
    source, _ = make_export()
    root = tmp_path / "analysis"
    real_rename = Path.rename
    def fail_run(self, target):
        if self.name == "run":
            raise OSError("simulated")
        return real_rename(self, target)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", fail_run)
        with pytest.raises(InputPreparationError, match="storage_error"):
            prepare_input(source, root)
    result = prepare_input(source, root)
    assert result["status"] == "ready"
    assert len(list((root / "bilibili-video-10001/runs").iterdir())) == 1


def test_reply_projection_conflict(make_export, tmp_path):
    source, batch = make_export()
    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    info = json.loads((batch / manifest["users"][0]["path"]).read_text(encoding="utf-8"))
    path = batch / info["comments_path"]
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["content"]["text"] = "different projection"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(InputPreparationError, match="invalid_export"):
        prepare_input(source, tmp_path / "analysis")



@pytest.mark.parametrize("value", [None, [], "wrong", 42])
def test_invalid_run_json_type(make_export, tmp_path, value):
    source, _ = make_export()
    root = tmp_path / "analysis"
    result = prepare_input(source, root)
    (Path(result["run_path"]) / "run.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(InputPreparationError, match="stored_run_invalid"):
        prepare_input(source, root)


def test_crash_after_commit_returns_same_run(make_export, tmp_path, monkeypatch):
    source, _ = make_export()
    root = tmp_path / "analysis"
    real_rename = Path.rename
    committed = []
    def interrupted(self, target):
        result = real_rename(self, target)
        if self.name == "run":
            committed.append(Path(target).name)
            raise OSError("caller lost response after commit")
        return result
    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", interrupted)
        with pytest.raises(InputPreparationError, match="storage_error"):
            prepare_input(source, root)
    result = prepare_input(source, root)
    assert result["analysis_run_id"] == committed[0]
    assert len(list((root / "bilibili-video-10001/runs").iterdir())) == 1


def test_changed_same_export_does_not_overwrite(make_export, tmp_path):
    source, batch = make_export()
    root = tmp_path / "analysis"
    first = prepare_input(source, root)
    for path in batch.rglob("comments.jsonl"):
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows[0]["content"]["text"] = "changed"
        # All copies of every comment are changed identically.
        for row in rows:
            row["content"]["text"] = "changed"
        path.write_bytes(("".join(json.dumps(row) + "\n" for row in rows)).encode())
    with pytest.raises(InputPreparationError, match="input_conflict"):
        prepare_input(source, root)
    assert Path(first["run_path"]).is_dir()


def test_preparation_lock_is_cross_process(tmp_path):
    import subprocess
    import sys

    from app.analysis_input.locking import preparation_lock
    code = ("from pathlib import Path; import sys; "
            "from app.analysis_input.locking import preparation_lock; "
            "guard=preparation_lock(Path(sys.argv[1])); guard.__enter__(); "
            "print('locked',flush=True); sys.stdin.readline(); guard.__exit__(None,None,None)")
    lock = tmp_path / "lock"
    child = subprocess.Popen([sys.executable, "-c", code, str(lock)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(InputPreparationError, match="lock_timeout"), preparation_lock(
            lock, timeout=0.1
        ):
            pass
    finally:
        child.communicate("release\n", timeout=10)
    assert child.returncode == 0
    with preparation_lock(lock, timeout=0.1):
        assert lock.exists()


def test_prepared_input_survives_source_cleanup(make_export, tmp_path):
    import shutil
    source, _ = make_export()
    result = prepare_input(source, tmp_path / "analysis")
    shutil.rmtree(source)
    assert (Path(result["input_path"]) / "manifest.json").is_file()
    assert (Path(result["run_path"]) / "context/README.md").is_file()


def test_root_output_in_git_must_be_ignored(make_export, tmp_path):
    import subprocess
    source, _ = make_export()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(InputPreparationError, match="storage_not_ignored"):
        prepare_input(source, repo / "analysis")
    assert not (repo / "analysis").exists()


def test_prepare_never_opens_network(make_export, tmp_path, monkeypatch):
    import socket
    source, _ = make_export()
    def reject(*args, **kwargs):
        raise AssertionError("network is forbidden during preparation")
    monkeypatch.setattr(socket.socket, "connect", reject)
    assert prepare_input(source, tmp_path / "analysis")["model_execution_authorized"] is False


@pytest.mark.parametrize("text", ["第一段\u2028第二段", "文字\u0085继续", "段落\u2029后文"])
def test_jsonl_only_physical_lf_separates_records(make_export, tmp_path, text):
    source, batch = make_export()
    for path in batch.rglob("comments.jsonl"):
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n")[:-1]]
        for row in rows:
            row["content"]["text"] = text
        path.write_bytes("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode())
    assert prepare_input(source, tmp_path / "analysis")["status"] == "ready"


def test_finite_large_extension_number_supported(make_export, tmp_path):
    source, batch = make_export()
    path = batch / "manifest.json"
    path.write_bytes(path.read_bytes().replace(b"{", b'{"extension":1e400,', 1))
    assert prepare_input(source, tmp_path / "analysis")["status"] == "ready"


def test_precise_cross_view_mismatch_rejected(make_export, tmp_path):
    source, batch = make_export()
    for path in batch.rglob("comments.jsonl"):
        value = b"0.123456789012345678901" if "users" in path.parts else b"0.123456789012345678902"
        lines = path.read_bytes().split(b"\n")[:-1]
        path.write_bytes(b"\n".join(line.replace(b"{", b'{"extra":' + value + b",", 1)
                                    for line in lines) + b"\n")
    with pytest.raises(InputPreparationError, match="invalid_export"):
        prepare_input(source, tmp_path / "analysis")


def test_empty_and_unknown_only_do_not_create_profiles(frozen_case, tmp_path):
    from copy import deepcopy
    from uuid import uuid4

    from app.comment_export.export import build_batch, write_json
    for unknown in (False, True):
        records, metadata = deepcopy(frozen_case)
        eid = str(uuid4())
        metadata.update(schema_version="2.0.0", export_id=eid)
        if not unknown:
            records = []
            metadata["_threads"] = {}
        for row in records:
            row.update(schema_version="2.0.0", export_id=eid)
            row["author"] = {"uid": None, "nickname": None}
        source = tmp_path / eid
        build_batch(records, metadata, source / "batches" / eid)
        write_json(source / "current.json", {"schema_version": "2.0.0",
                   "export_id": eid, "video_id": metadata["video_id"],
                   "batch_path": "batches/" + eid})
        result = prepare_input(source, tmp_path / "analysis")
        assert result["status"] == "no_analyzable_users"
        assert list((Path(result["run_path"]) / "users").iterdir()) == []
