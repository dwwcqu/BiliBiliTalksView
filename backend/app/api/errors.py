"""Stable HTTP errors never echo user bodies, credentials or database exceptions."""

from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.jobs.errors import JobError
from app.storage.errors import StorageError

from .rate_limits import RateLimited
from .responses import APIError, response


def install_handlers(app):
    @app.exception_handler(APIError)
    async def api_error(request, exc):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return response({"error": exc.code}, exc.status, headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return response({"error": "invalid_request"}, 422)

    @app.exception_handler(RateLimited)
    async def limited(request, exc):
        return response(
            {"error": "rate_limited", "limit": exc.kind, "retry_after": exc.retry_after},
            429,
            {"Retry-After": str(exc.retry_after)},
        )

    @app.exception_handler(JobError)
    @app.exception_handler(StorageError)
    async def domain_error(request, exc):
        code = exc.code
        if code in {"job_not_found", "request_not_found", "video_not_found", "not_published"}:
            status = 404
        elif code in {"state_expired", "already_imported_expired", "work_expired"}:
            status = 410
        elif code in {"queue_full"}:
            status = 429
        elif code.startswith("invalid_") or code in {"unsupported_schema_version"}:
            status = 422
        elif code in {
            "state_conflict",
            "budget_exhausted",
            "baseline_changed",
            "import_busy",
            "state_not_readable",
            "state_not_publishable",
        }:
            status = 409
        else:
            return response({"error": "internal_error"}, 500)
        return response({"error": code}, status)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return response({"error": "database_unavailable"}, 503)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return response({"error": "internal_error"}, 500)
