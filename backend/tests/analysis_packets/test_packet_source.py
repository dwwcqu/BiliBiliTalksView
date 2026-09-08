from pathlib import Path

import pytest

from app.analysis_packets.budget import Budget
from app.analysis_packets.context import select_context
from app.analysis_packets.source import load_source


@pytest.mark.parametrize("version", ["1.0.0", "2.0.0"])
def test_load_prepared_source(prepared_case, version):
    root, record = prepared_case(version)
    source = load_source(root, record["analysis_run_id"], record["video_id"])
    assert source.export_schema_version == version
    assert set(source.users) == {"10", "20"}
    comments, gaps = select_context(source, ("100", "200"))
    assert {r["comment_id"] for r in comments if r["input_role"] == "target"} == {"100", "200"}
    assert "102" in {r["comment_id"] for r in comments}
    assert all("nickname" not in r and "like_count" not in r for r in comments)
    assert gaps == []


def test_damaged_prepared_context_rejected(prepared_case):
    root, record = prepared_case()
    (Path(record["run_path"]) / "context/README.md").write_text("changed")
    with pytest.raises(ValueError, match="invalid_prepared_input"):
        load_source(root, record["analysis_run_id"], record["video_id"])


@pytest.mark.parametrize("value", [True, 0, -1])
def test_invalid_budget(value):
    with pytest.raises(ValueError):
        Budget(value, 100, 1000, 2)


def test_budget_cannot_exceed_window():
    with pytest.raises(ValueError):
        Budget(900, 200, 1000, 2)
