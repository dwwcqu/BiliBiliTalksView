"""Offline commands only; no model, credentials or database connection paths."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from app.comment_export.contract import parse_json

from .budget import Budget
from .builder import build_primary, prepare_resources
from .errors import PacketError
from .publication import publish_assembly, validate_published
from .source import load_source


def main(argv=None):
    parser = argparse.ArgumentParser(description="Assemble or validate offline analysis packets")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("assemble-primary")
    for name in ("analysis-root", "video-id", "prepared-run-id", "resources-config"):
        build.add_argument("--" + name, required=True)
    for name in (
        "max-input-tokens",
        "reserved-output-tokens",
        "context-window",
        "max-active-members",
    ):
        build.add_argument("--" + name, required=True, type=int)
    validate = sub.add_parser("validate")
    for name in ("analysis-root", "run-id", "manifest-id"):
        validate.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "assemble-primary":
            bundle = load_source(Path(args.analysis_root), args.prepared_run_id, args.video_id)
            config = parse_json(Path(args.resources_config).read_text(encoding="utf-8"))
            resources, files = prepare_resources(bundle, config)
            budget = Budget(
                args.max_input_tokens,
                args.reserved_output_tokens,
                args.context_window,
                args.max_active_members,
            )
            assembly = build_primary(bundle, str(uuid4()), resources, budget, resource_files=files)
            result = publish_assembly(Path(args.analysis_root), assembly, bundle)
        else:
            result = validate_published(Path(args.analysis_root), args.run_id, args.manifest_id)
    except (PacketError, OSError, ValueError, TypeError, KeyError) as exc:
        code = (
            exc.code
            if isinstance(exc, PacketError)
            else (
                str(exc)
                if isinstance(exc, ValueError)
                and str(exc)
                in {
                    "invalid_prepared_input",
                    "invalid_resources",
                    "invalid_budget",
                    "input_budget_exceeded",
                }
                else "invalid_input"
            )
        )
        print(json.dumps({"status": "error", "error": code}))
        return 3 if code in {"packet_publication_failed", "lock_timeout"} else 2
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
