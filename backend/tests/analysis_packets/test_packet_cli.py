import json
import socket
from pathlib import Path

from app.analysis_packets.cli import main


def test_cli_assemble_and_validate(prepared_case, resource_config, tmp_path, capsys, monkeypatch):
    root, record = prepared_case()
    config = tmp_path / "resources.json"
    config.write_text(json.dumps(resource_config), encoding="utf-8")

    def forbid(*args, **kwargs):
        raise AssertionError("No network calls allowed")

    monkeypatch.setattr(socket.socket, "connect", forbid)
    args = [
        "assemble-primary",
        "--analysis-root",
        str(root),
        "--video-id",
        record["video_id"],
        "--prepared-run-id",
        record["analysis_run_id"],
        "--resources-config",
        str(config),
        "--max-input-tokens",
        "100000",
        "--reserved-output-tokens",
        "1000",
        "--context-window",
        "110000",
        "--max-active-members",
        "2",
    ]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "offline_prepared"
    assert result["model_execution_authorized"] is False
    assert (
        main(
            [
                "validate",
                "--analysis-root",
                str(root),
                "--run-id",
                result["run_id"],
                "--manifest-id",
                result["manifest_id"],
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "valid_offline"
    assert list((Path(result["run_path"]) / "users").iterdir()) == []


def test_cli_rejects_missing_preparation(tmp_path, capsys):
    assert (
        main(
            [
                "assemble-primary",
                "--analysis-root",
                str(tmp_path),
                "--video-id",
                "bilibili:video:1",
                "--prepared-run-id",
                "prep-" + "0" * 64,
                "--resources-config",
                str(tmp_path / "missing"),
                "--max-input-tokens",
                "1",
                "--reserved-output-tokens",
                "1",
                "--context-window",
                "2",
                "--max-active-members",
                "1",
            ]
        )
        == 2
    )
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "invalid_prepared_input"
