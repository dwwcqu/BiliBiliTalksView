"""Verify a prepared run without reading a live export or opening a database."""

import hashlib
import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.analysis_input.digest import ALGORITHM, exact_digest
from app.analysis_input.storage import fingerprint, safe_child
from app.comment_export.contract import parse_json
from app.comment_export.validation import read_json, safe_path, validate_batch

from .codec import loads


@dataclass(frozen=True)
class SourceBundle:
    video_id: str
    export_id: str
    export_schema_version: str
    prepared_run_id: str
    input_fingerprint: str
    _data: dict[str, Any] = field(repr=False)

    @property
    def manifest(self):
        return deepcopy(self._data["manifest"])

    @property
    def comments_by_id(self):
        return deepcopy(self._data["comments"])

    @property
    def users(self):
        return deepcopy(self._data["users"])

    @property
    def threads(self):
        return deepcopy(self._data["threads"])

    @property
    def context_bytes(self):
        return dict(self._data["context"])


def load_source(analysis_root: Path, prepared_run_id: str, video_id: str) -> SourceBundle:
    try:
        if not re.fullmatch(r"bilibili:video:[1-9][0-9]*", video_id):
            raise ValueError("identity")
        if not re.fullmatch(r"prep-[0-9a-f]{64}", prepared_run_id):
            raise ValueError("identity")
        root = Path(analysis_root).resolve()
        video = safe_child(root, "bilibili-video-" + video_id.rsplit(":", 1)[1])
        run = safe_child(video, "runs/" + prepared_run_id)
        record = parse_json(safe_child(run, "run.json").read_text(encoding="utf-8"))
        request = record["request"]
        if (
            request["video_id"] != video_id
            or record["analysis_run_id"] != prepared_run_id
            or "prep-" + fingerprint(request) != prepared_run_id
            or record["input_digest_algorithm"] != ALGORITHM
            or record["model_execution_authorized"] is not False
            or record["status"] not in {"ready", "waiting_policy", "no_analyzable_users"}
        ):
            raise ValueError("record")
        expected = f"bilibili-video-{video_id.rsplit(':', 1)[1]}/inputs/{request['export_id']}"
        if record["input_path"] != expected:
            raise ValueError("path")
        directory = safe_child(root, expected)
        manifest = validate_batch(directory, analysis_readme=True)
        if (
            manifest["video_id"] != video_id
            or manifest["export_id"] != request["export_id"]
            or exact_digest(directory) != request["input_fingerprint"]
            or record["coverage"] != manifest["coverage"]
            or record["counts"] != manifest["counts"]
            or fingerprint(record["context_hashes"]) != request["context_fingerprint"]
        ):
            raise ValueError("input")
        context_root = safe_child(run, "context")
        if {p.name for p in context_root.iterdir()} != set(record["context_hashes"]):
            raise ValueError("context")
        context = {}
        for name, digest in record["context_hashes"].items():
            content = safe_child(context_root, name).read_bytes()
            content.decode("utf-8")
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError("context")
            context[name] = content
        comments, threads, users = {}, {}, {}
        for entry in manifest["threads"]:
            info = read_json(safe_path(directory, entry["path"]), "thread")
            rows = [
                loads(line)
                for line in safe_path(directory, info["comments_path"])
                .read_text(encoding="utf-8")
                .split("\n")[:-1]
            ]
            threads[info["root_id"]] = tuple(row["comment_id"] for row in rows)
            for row in rows:
                comments[row["comment_id"]] = row
                if row["author"]["uid"] is not None:
                    users.setdefault(row["author"]["uid"], []).append(row["comment_id"])
        return SourceBundle(
            video_id,
            manifest["export_id"],
            manifest["schema_version"],
            prepared_run_id,
            request["input_fingerprint"],
            {
                "manifest": manifest,
                "context": context,
                "comments": comments,
                "threads": threads,
                "users": users,
            },
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError("invalid_prepared_input") from exc
