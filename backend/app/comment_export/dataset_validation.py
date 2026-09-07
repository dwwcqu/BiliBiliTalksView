"""Validate source logical documents without rebuilding projections."""
import re
from collections.abc import Mapping
from copy import deepcopy
from math import isfinite
from pathlib import PurePosixPath

from .contract import ContractError, validate_record
from .layout import nickname, order, user_directory, user_folder


def _json_value(value: object) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise ContractError("invalid_json")


def validate_documents(documents: Mapping[str, object]) -> dict:
    """Check original document values and return an independent manifest."""
    for path in documents:
        if (not isinstance(path, str) or not path or "\\" in path or ":" in path
                or "\x00" in path or PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts
                or PurePosixPath(path).as_posix() != path):
            raise ContractError("unsafe_path")
    for path in documents:
        if any(parent.as_posix() in documents for parent in PurePosixPath(path).parents):
            raise ContractError("file_directory_conflict")

    def get(path):
        if path not in documents:
            raise ContractError("missing_document")
        return documents[path]

    def document(path, kind):
        value = get(path)
        _json_value(value)
        validate_record(kind, value)
        return value

    def lines(path, kind="comment"):
        values = get(path)
        if not isinstance(values, list):
            raise ContractError("invalid_jsonl")
        for value in values:
            _json_value(value)
            validate_record(kind, value)
        return values

    manifest = document("manifest.json", "manifest")
    if manifest["schema_version"].startswith("2."):
        for path in documents:
            parts = PurePosixPath(path).parts
            if (any(not re.fullmatch(r"[a-z0-9_-]+", part) for part in parts[:-1])
                    or not re.fullmatch(r"[A-Za-z0-9_.-]+", parts[-1])):
                raise ContractError("nonportable_export_path")
    if get("README.md") != "":
        raise ContractError("nonempty_readme")
    identity = {k: manifest[k] for k in ("schema_version", "video_id", "export_id")}
    projections = []
    seen_paths = {"manifest.json", "README.md"}
    thread_infos = []
    for group, kind, key in (("threads", "thread", "root_id"), ("users", "user", "uid")):
        seen, ids = {}, set()
        for entry in manifest[group]:
            if entry[key] in ids or entry["path"] in seen_paths:
                raise ContractError("duplicate_index")
            ids.add(entry[key])
            seen_paths.add(entry["path"])
            info = document(entry["path"], kind)
            if info[key] != entry[key] or any(info[k] != v for k, v in identity.items()):
                raise ContractError("index_identity_mismatch")
            path = info["comments_path"]
            if path in seen_paths:
                raise ContractError("duplicate_path")
            seen_paths.add(path)
            rows = lines(path)
            if info["comment_count"] != len(rows) or not rows:
                raise ContractError("invalid_comment_count")
            for row in rows:
                if any(row[k] != v for k, v in identity.items()) or row["comment_id"] in seen:
                    raise ContractError("invalid_comment_identity")
                owner = row["root_id"] if kind == "thread" else row["author"]["uid"]
                if owner != entry[key]:
                    raise ContractError("wrong_projection_owner")
                seen[row["comment_id"]] = row
            expected = sorted(rows, key=order if kind == "user"
                              else lambda row: (row["kind"] != "root", *order(row)))
            if rows != expected:
                raise ContractError("invalid_order")
            if kind == "user":
                cov = manifest["coverage"]
                if (info["coverage_status"] != cov["status"] or info["context_status"] != cov["context_status"]
                        or info["reasons"] != cov["reasons"]):
                    raise ContractError("inconsistent_user_coverage")
                if set(info["thread_ids"]) != {r["root_id"] for r in rows}:
                    raise ContractError("invalid_user_threads")
                if info["display_nickname"] != nickname(rows):
                    raise ContractError("invalid_display_nickname")
                folder = user_directory(manifest["schema_version"]) + "/" + user_folder(entry[key], nickname(rows), manifest["schema_version"])
                if path != folder + "/comments.jsonl" or entry["path"] != folder + "/user.json":
                    raise ContractError("invalid_user_path")
            else:
                thread_infos.append((info, rows))
        projections.append(seen)
    if projections[0] != projections[1]:
        raise ContractError("projection_mismatch")
    all_rows = projections[0]
    for info, rows in thread_infos:
        row_ids = {r["comment_id"] for r in rows}
        root = info["root_id"]
        missing = {r["parent_id"] for r in rows if r["parent_id"] and r["parent_id"] not in row_ids}
        if root not in row_ids:
            missing.add(root)
        unknown = {r["comment_id"] for r in rows if r["kind"] == "reply" and not r["parent_id"]}
        cov = info["coverage"]
        if set(cov["missing_parent_ids"]) != missing or set(cov["unknown_parent_comment_ids"]) != unknown:
            raise ContractError("incorrect_context_gaps")
        if (info["reply_count"] != sum(r["kind"] == "reply" for r in rows)
                or info["participant_count"] != len({r["author"]["uid"] for r in rows} - {None})
                or info["unknown_author_comment_count"] != sum(r["author"]["uid"] is None for r in rows)):
            raise ContractError("invalid_thread_counts")
        expected_author = all_rows[root]["author"] if root in row_ids else {"uid": None, "nickname": None}
        if info["root_author"] != expected_author:
            raise ContractError("invalid_root_author")
        required = set()
        if missing:
            required.add("missing_parent")
        if unknown:
            required.add("unknown_parent")
        if any(r["author"]["uid"] is None for r in rows):
            required.add("unknown_author")
        if any(r["content"]["text"] is None for r in rows):
            required.add("missing_content")
        if any(parent in all_rows for parent in missing):
            required.add("cross_thread_parent")
        if required and (cov["context_status"] != "gaps" or not required <= set(cov["reasons"])):
            raise ContractError("hidden_context_gaps")
        if cov["reasons"] and (manifest["coverage"]["context_status"] != "gaps"
                              or not set(cov["reasons"]) <= set(manifest["coverage"]["reasons"])):
            raise ContractError("hidden_batch_gaps")
        if manifest["coverage"]["status"] == "verified" and cov["pagination_status"] != "verified":
            raise ContractError("invalid_thread_coverage")
    exceptions = []
    if manifest["unclassified_path"]:
        seen_paths.add(manifest["unclassified_path"])
        exceptions = lines(manifest["unclassified_path"], "unclassified")
    counts = {"root_comments": sum(r["kind"] == "root" for r in all_rows.values()),
              "replies": sum(r["kind"] == "reply" for r in all_rows.values()),
              "comments": len(all_rows),
              "known_users": len({r["author"]["uid"] for r in all_rows.values()} - {None}),
              "unknown_author_comments": sum(r["author"]["uid"] is None for r in all_rows.values()),
              "unclassified_records": len(exceptions)}
    if counts != manifest["counts"]:
        raise ContractError("invalid_manifest_counts")
    if set(documents) != seen_paths:
        raise ContractError("unindexed_files")
    return deepcopy(manifest)
