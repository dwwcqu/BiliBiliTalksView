"""HTTP foundation. Collection and model analysis are not implemented yet."""

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def create_app(static_directory: Path | None = None) -> FastAPI:
    production = os.getenv("APP_ENV", "development") == "production"
    app = FastAPI(
        title="BiliBili Talk View API",
        version="0.1.0",
        docs_url=None if production else "/api/docs",
        redoc_url=None,
        openapi_url=None if production else "/api/openapi.json",
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "bilibili-talk-view", "version": "0.1.0"}

    # Reserve /api before mounting the web application.
    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def unknown_api(path: str):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": "Not Found"})

    web_dist = static_directory if static_directory is not None else ROOT / "frontend" / "dist"
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    return app


app = create_app()
