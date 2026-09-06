"""Local PostgreSQL import/query/export commands with safe diagnostic codes."""

import argparse
import json
import os
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from app.comment_export.contract import ContractError

from .connection import open_connection
from .errors import StorageError
from .exporter import export_state
from .frozen import freeze_batch
from .importer import import_batch
from .publication import publish_state
from .queries import list_thread_comments, list_user_comments


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PostgreSQL discussion storage")
    sub = parser.add_subparsers(dest="command", required=True)
    importer = sub.add_parser("import")
    importer.add_argument("--input", type=Path, required=True)
    importer.add_argument("--work-dir", type=Path, default=Path("data/import-work"))
    importer.add_argument("--publish", action="store_true")
    publish = sub.add_parser("publish")
    publish.add_argument("--video-id", required=True)
    publish.add_argument("--state-id", required=True)
    export = sub.add_parser("export")
    export.add_argument("--video-id", required=True)
    export.add_argument("--state-id")
    export.add_argument("--output", type=Path, required=True)
    query = sub.add_parser("query")
    query.add_argument("--state-id", required=True)
    owner = query.add_mutually_exclusive_group(required=True)
    owner.add_argument("--root-id")
    owner.add_argument("--uid")
    owner.add_argument("--unknown-author", action="store_true")
    query.add_argument("--limit", type=int, default=100)
    query.add_argument("--cursor")
    args = parser.parse_args(argv)
    try:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise StorageError("database_url_required")
        if args.command == "import":
            # Do not hold a database connection while copying source files.
            with freeze_batch(args.input, args.work_dir) as frozen, open_connection(url) as conn:
                result = import_batch(conn, frozen, publish=args.publish)
        else:
            with open_connection(url) as conn:
                if args.command == "publish":
                    result = publish_state(conn, args.video_id, args.state_id)
                elif args.command == "export":
                    result = {
                        "path": str(export_state(conn, args.video_id, args.output, args.state_id))
                    }
                elif args.root_id:
                    result = list_thread_comments(
                        conn, args.state_id, args.root_id, args.limit, args.cursor
                    )
                else:
                    result = list_user_comments(
                        conn, args.state_id, args.uid, args.limit, args.cursor
                    )
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        if (
            args.command == "publish" or (args.command == "import" and args.publish)
        ) and not result["published"]:
            return 3
        return 0
    except StorageError as exc:
        print(json.dumps({"error": exc.code}))
        if exc.code in {
            "import_busy",
            "already_imported_expired",
            "state_expired",
            "stale_batch",
            "ambiguous_replacement",
            "not_published",
            "state_not_publishable",
        }:
            return 3
        return 2 if exc.code.startswith("invalid_") or exc.code == "export_id_conflict" else 4
    except (ContractError, ValueError):
        print(json.dumps({"error": "invalid_export_or_argument"}))
        return 2
    except (SQLAlchemyError, OSError):
        print(json.dumps({"error": "database_or_file_error"}))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
