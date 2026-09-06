import pytest

from app.storage.cli import main


def test_database_url_required(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["query", "--state-id", "12345678-1234-4234-8234-123456789012", "--uid", "1"]) == 4
    assert "database_url_required" in capsys.readouterr().out


def test_query_owner_required():
    with pytest.raises(SystemExit) as exc:
        main(["query", "--state-id", "12345678-1234-4234-8234-123456789012"])
    assert exc.value.code == 2
