from fastapi.testclient import TestClient

from app.main import create_app


def test_web_and_api_share_one_origin(tmp_path):
    (tmp_path / "index.html").write_text("<h1>BiliBili Talk View</h1>", encoding="utf-8")
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert "BiliBili Talk View" in client.get("/").text
        assert client.get("/api/missing").status_code == 404


def test_production_disables_api_documentation(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "production")
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/docs").status_code == 404
        assert client.get("/api/openapi.json").status_code == 404
