import json

from app.analysis_input.cli import main


def test_cli_partial_json(make_export, tmp_path, capsys):
    source, _ = make_export(partial=True)
    assert main(["prepare", "--export-container", str(source),
                 "--analysis-root", str(tmp_path / "analysis")]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "waiting_policy"


def test_cli_no_pointer(tmp_path, capsys):
    assert main(["prepare", "--export-container", str(tmp_path / "missing"),
                 "--analysis-root", str(tmp_path / "analysis")]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "not_published"
