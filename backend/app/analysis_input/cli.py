"""Command line entry for local input preparation, never model execution."""
import argparse
import json
from pathlib import Path

from .errors import InputPreparationError
from .storage import prepare_input


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare immutable analysis input")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--export-container", type=Path, required=True)
    prepare.add_argument("--analysis-root", type=Path, required=True)
    prepare.add_argument("--allow-partial", action="store_true")
    prepare.add_argument("--context", action="append", default=[], metavar="NAME=PATH")
    args = parser.parse_args(argv)
    try:
        context = {}
        for item in args.context:
            name, separator, source = item.partition("=")
            if not separator or not source or name in context:
                raise InputPreparationError("invalid_context")
            context[name] = Path(source)
        result = prepare_input(args.export_container, args.analysis_root,
                               context_files=context, allow_partial=args.allow_partial)
    except InputPreparationError as exc:
        code = str(exc)
        print(json.dumps({"status": "error", "error": code}))
        return 3 if code in {"storage_error", "lock_timeout", "storage_not_ignored",
                             "stored_input_invalid", "stored_run_invalid"} else 2
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
