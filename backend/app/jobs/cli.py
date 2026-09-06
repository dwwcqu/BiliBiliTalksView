"""Local operator CLI; HTTP authentication and user rate limits are a later layer."""

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from app.comment_export.contract import ContractError

from . import repository
from .errors import JobError
from .maintenance import purge_metadata
from .runner import Worker


def public(value):
    hidden = {"owner_token", "worker_token", "owner_backend_pid", "lease_until"}
    if isinstance(value, dict):
        return {key: public(item) for key, item in value.items() if key not in hidden}
    if isinstance(value, list):
        return [public(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def main(argv=None):
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    parser = argparse.ArgumentParser(description="Persistent Bilibili queue and local worker")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("submit").add_argument("--url", required=True)
    for name in ("refresh", "video"):
        item = sub.add_parser(name)
        item.add_argument("--video-id", required=True)
        if name == "refresh":
            item.add_argument("--mode", choices=("auto", "full"), default="auto")
    for name in ("job", "cancel", "retry", "recover"):
        sub.add_parser(name).add_argument("--job-id", required=True)
    for name in ("request", "request-retry", "request-recover"):
        sub.add_parser(name).add_argument("--request-id", required=True)
    sub.add_parser("source-revalidate")
    worker = sub.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    worker.add_argument(
        "--data-root", type=Path, default=Path(os.getenv("JOB_DATA_ROOT", "data/jobs"))
    )
    args = parser.parse_args(argv)
    engine = None
    try:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise JobError("database_url_required")
        engine = create_engine(
            url, hide_parameters=True, pool_pre_ping=True, connect_args={"connect_timeout": 5}
        )
        if args.command == "worker":
            with Worker(engine, args.data_root) as running:
                last_purge = 0.0
                while True:
                    if time.monotonic() - last_purge > 3600:
                        purge_metadata(engine, args.data_root)
                        last_purge = time.monotonic()
                    result = running.run_once()
                    if result["status"] != "idle" or args.once:
                        print(json.dumps(public(result), ensure_ascii=True), flush=True)
                    if args.once or result["status"] == "interrupted":
                        return 3 if result["status"] == "interrupted" else 0
                    if result["status"] == "idle":
                        time.sleep(2)
        with engine.connect() as conn:
            if args.command == "submit":
                result = repository.submit(conn, args.url)
            elif args.command == "refresh":
                result = repository.request_refresh(conn, args.video_id, mode=args.mode)
            elif args.command == "video":
                result = repository.video_view(conn, args.video_id)
            elif args.command == "job":
                result = repository.get_job(conn, args.job_id)
            elif args.command == "request":
                result = repository.get_request(conn, args.request_id)
            elif args.command in {"cancel", "retry", "recover"}:
                result = repository.control_job(conn, args.job_id, args.command)
            elif args.command in {"request-retry", "request-recover"}:
                result = repository.control_request(
                    conn, args.request_id, args.command.split("-")[1]
                )
            else:
                result = repository.request_revalidation(conn)
        print(json.dumps(public(result), ensure_ascii=True))
        return 0
    except KeyboardInterrupt:
        return 130
    except JobError as exc:
        print(json.dumps({"error": exc.code}))
        return 2
    except (SQLAlchemyError, OSError, ValueError, ContractError):
        print(json.dumps({"error": "worker_operation_failed"}))
        return 3
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
