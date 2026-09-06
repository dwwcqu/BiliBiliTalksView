"""Standalone collection/export commands; no browser or agent runtime required."""
import argparse
import hashlib
import json
import os
import re
import shutil
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from .access_control import AccessClient, AccessControl, AccessControlError
from .checkpoint import Checkpoint
from .collector import collect, now
from .contract import ContractError
from .export import LATEST_SCHEMA_VERSION, build_batch
from .publication import exclusive_lock, publish_batch
from .recovery import diagnose, probe, recover
from .source import CollectionStopped, login_check
from .validation import validate_batch


def _control_dir() -> Path:
    return Path(os.environ.get("BILIBILI_CONTROL_DIR", "data/collection-control"))


def _client() -> httpx.Client:
    headers = {}
    cookie_path = os.environ.get("BILIBILI_COOKIE_FILE")
    if cookie_path:
        cookie = Path(cookie_path).read_text(encoding="utf-8-sig").strip()
        if cookie.lower().startswith("cookie:"):
            cookie = cookie[7:].strip()
        if not cookie or "\n" in cookie or "\r" in cookie:
            raise ContractError("invalid_cookie_file")
        headers["Cookie"] = cookie
    return AccessClient(headers=headers, timeout=20, follow_redirects=False, verify=True,
                        access_policy=AccessControl(_control_dir()))


def export_work(work_dir: Path, output: Path) -> tuple[Path, dict]:
    cp = Checkpoint(work_dir / "work.sqlite3")
    try:
        rows, metadata = cp.freeze()
    finally:
        cp.close()
    if "video_id" not in metadata:
        raise ContractError("no_collection")
    metadata = deepcopy(metadata)
    export_id = str(uuid4())
    metadata["export_id"] = export_id
    metadata["schema_version"] = LATEST_SCHEMA_VERSION
    metadata["exported_at"] = now()
    rows = [dict(row, export_id=export_id, schema_version=LATEST_SCHEMA_VERSION) for row in rows]
    container = output / ("bilibili-video-" + metadata["source"]["aid"])
    # Preserve the most recent working export alongside the current published batch.
    working = container / ".work" / export_id
    build_batch(rows, metadata, working)
    manifest = validate_batch(working)
    try:
        target = publish_batch(working, container)
    except ContractError as exc:
        if str(exc) != "partial_not_published":
            raise
        target = working
    # Only clear our own validated work siblings, never the user's output root.
    for sibling in working.parent.iterdir():
        if sibling != working and sibling.is_dir() and not sibling.is_symlink():
            try:
                UUID(sibling.name)
            except ValueError:
                continue
            resolved = sibling.resolve()
            if resolved.is_relative_to((container / ".work").resolve()):
                shutil.rmtree(resolved)
    if target != working:
        shutil.rmtree(working)
    return target, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect a supplied Bilibili video and export protocol 2.0.0")
    sub = parser.add_subparsers(dest="command", required=True)
    collection = sub.add_parser("collect")
    collection.add_argument("--url", required=True)
    collection.add_argument("--work-dir", type=Path, default=Path("data/collection"))
    collection.add_argument("--output", type=Path, default=Path("data/exports"))
    collection.add_argument("--resume", action="store_true")
    collection.add_argument("--max-requests", type=int, default=12000)
    refresh_parser = sub.add_parser("refresh", help="Refresh from a frozen baseline; no hourly scheduler")
    refresh_parser.add_argument("--url", required=True)
    refresh_parser.add_argument("--work-dir", type=Path, required=True, help="Exact new task directory")
    refresh_parser.add_argument("--output", type=Path, default=Path("data/exports"))
    refresh_parser.add_argument("--resume", action="store_true")
    refresh_parser.add_argument("--mode", choices=("auto", "full"), default="auto")
    refresh_parser.add_argument("--full-interval-hours", type=float, default=24)
    refresh_parser.add_argument("--max-requests", type=int, default=12000)
    baseline = refresh_parser.add_mutually_exclusive_group()
    baseline.add_argument("--baseline-work", type=Path)
    baseline.add_argument("--baseline-batch", type=Path)
    export = sub.add_parser("export")
    export.add_argument("--work-dir", type=Path, required=True)
    export.add_argument("--output", type=Path, default=Path("data/exports"))
    validate = sub.add_parser("validate")
    validate.add_argument("--batch", type=Path, required=True)
    diagnosis = sub.add_parser("diagnose", help="Read local state only; no network or credential reads")
    diagnosis.add_argument("--work-dir", type=Path, required=True, help="Exact task directory, not collection root")
    for command in ("probe", "recover"):
        item = sub.add_parser(command, help="Explicit network operation subject to cooldown")
        item.add_argument("--work-dir", type=Path, required=True, help="Exact task directory")
        item.add_argument("--next-pending", action="store_true", help="Select next pending page for a legacy task")
        if command == "recover":
            item.add_argument("--output", type=Path, required=True)
    sub.add_parser("login-check", help="One login observation; does not unblock any task")
    args = parser.parse_args(argv)
    try:
        if args.command == "diagnose":
            print(json.dumps(diagnose(args.work_dir, _control_dir()), ensure_ascii=True))
            return 0
        if args.command in {"probe", "recover", "login-check"}:
            with _client() as client:
                if args.command == "login-check":
                    status = login_check(client)
                    detail = getattr(client, "last_diagnostic", None)
                    print(json.dumps({"login_status": status, "diagnostic": detail.to_public_dict() if detail else None}))
                    return 0 if status == "logged_in" else 3
                if args.command == "probe":
                    result = probe(args.work_dir, client, args.next_pending)
                    print(json.dumps(result, ensure_ascii=True))
                    return 0
                result = recover(args.work_dir, client, args.output, args.next_pending)
                print(json.dumps(result, ensure_ascii=True))
                if result["export"].get("error"):
                    return 4
                return 0 if result["collection"]["finished"] and not result["collection"]["blocked"] and result["collection"]["coverage"]["status"] == "verified" else 3
        if args.command == "validate":
            manifest = validate_batch(args.batch)
            print(json.dumps({"status": "valid", "export_id": manifest["export_id"],
                              "coverage": manifest["coverage"], "counts": manifest["counts"]}))
            return 0
        if args.command == "refresh":
            from .refresh import refresh

            with exclusive_lock(args.work_dir / ".collect.lock"), _client() as client:
                _rows, metadata = refresh(
                    args.url, args.work_dir, client, args.max_requests, args.resume,
                    baseline_work=args.baseline_work, baseline_batch=args.baseline_batch,
                    mode=args.mode, full_interval_hours=args.full_interval_hours,
                )
                target, manifest = export_work(args.work_dir, args.output)
            info = metadata["_refresh"]
            print(json.dumps({"path": str(target), "work_dir": str(args.work_dir),
                "mode": info["mode"], "stats": info.get("stats", {}),
                "coverage": manifest["coverage"], "counts": manifest["counts"]}))
            return 0 if manifest["coverage"]["status"] == "verified" else 3
        if args.command == "collect":
            if args.max_requests < 1:
                raise ContractError("invalid_request_budget")
            parsed = urlsplit(args.url)
            key = hashlib.sha256((parsed.netloc + parsed.path.rstrip("/")).encode()).hexdigest()[:20]
            work = args.work_dir / key
            with exclusive_lock(work / ".collect.lock"), _client() as client:
                _rows, metadata = collect(args.url, work, client, args.max_requests, args.resume)
                target, manifest = export_work(work, args.output)
            print(json.dumps({"path": str(target), "work_dir": str(work),
                              "coverage": manifest["coverage"], "counts": manifest["counts"]}))
            return 0 if metadata["coverage"]["status"] == "verified" else 3
        with exclusive_lock(args.work_dir / ".collect.lock"):
            target, manifest = export_work(args.work_dir, args.output)
        print(json.dumps({"path": str(target), "coverage": manifest["coverage"],
                          "counts": manifest["counts"]}))
        return 0
    except CollectionStopped as exc:
        print(json.dumps({"error": exc.reason, "diagnostic": exc.detail.to_public_dict() if exc.detail else None}))
        return 3
    except AccessControlError as exc:
        try:
            control = AccessControl(_control_dir()).read_status()
        except AccessControlError:
            control = None
        print(json.dumps({"error": exc.reason, "control": control}))
        return 3
    except ContractError as exc:
        reason = str(exc)
        if reason == "busy":
            print(json.dumps({"error": "task_busy"}))
            return 3
        print(json.dumps({"error": reason if re.fullmatch(r"[a-z][a-z0-9_]*", reason) else "operation_failed"}))
        return 2
    except (OSError, ValueError):
        # Do not render source exceptions, Cookie values, or user comment bodies.
        print(json.dumps({"error": "invalid_export" if args.command == "validate" else "operation_failed"}))
        return 2 if args.command == "validate" else 4


if __name__ == "__main__":
    raise SystemExit(main())
