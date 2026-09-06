"""Discussion API and static web host; source collection runs in the separate worker."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine

from app.api.errors import install_handlers
from app.api.responses import SafeJSONResponse
from app.api.routes import router
from app.api.security import BodyLimitMiddleware, trusted_networks

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def create_app(
    static_directory: Path | None = None,
    *,
    database_engine=None,
    admin_token: str | None = None,
    rate_limit_secret: str | None = None,
    trusted_proxies=None,
) -> FastAPI:
    production = os.getenv("APP_ENV", "development") == "production"
    own_engine = database_engine is None and bool(os.getenv("DATABASE_URL"))
    engine = database_engine
    if own_engine:
        engine = create_engine(
            os.environ["DATABASE_URL"],
            hide_parameters=True,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5, "options": "-cstatement_timeout=5000"},
        )

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if own_engine:
                engine.dispose()

    app = FastAPI(
        lifespan=lifespan,
        default_response_class=SafeJSONResponse,
        title="BiliBili Talk View API",
        version="0.1.0",
        docs_url=None if production else "/api/docs",
        redoc_url=None,
        openapi_url=None if production else "/api/openapi.json",
    )

    app.state.database_engine = engine
    app.state.admin_token = os.getenv("ADMIN_TOKEN", "") if admin_token is None else admin_token
    app.state.rate_limit_secret = (
        os.getenv("RATE_LIMIT_SECRET", "") if rate_limit_secret is None else rate_limit_secret
    )
    app.state.trusted_proxies = trusted_networks(
        os.getenv("TRUSTED_PROXIES", "") if trusted_proxies is None else trusted_proxies
    )
    app.add_middleware(BodyLimitMiddleware)
    install_handlers(app)
    app.include_router(router)

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
