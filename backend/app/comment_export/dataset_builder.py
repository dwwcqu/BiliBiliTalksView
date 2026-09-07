"""Derive the existing export projections without creating files."""
from collections import defaultdict
from copy import deepcopy

from .contract import ContractError, validate_record
from .dataset import ValidatedDataset
from .layout import nickname, order, thread_directory, user_directory, user_folder


def build_dataset(records: list[dict], manifest: dict) -> ValidatedDataset:
    evidence = deepcopy(manifest)
    manifest = deepcopy(manifest)
    source_thread_index = {entry["root_id"]: entry for entry in manifest.get("threads", [])}
    source_user_index = {entry["uid"]: entry for entry in manifest.get("users", [])}
    manifest.pop("_refresh", None)
    thread_states = manifest.pop("_threads", {})
    unclassified = manifest.pop("_unclassified", [])
    identity = {k: manifest[k] for k in ("schema_version", "video_id", "export_id")}
    unique = {}
    for row in records:
        validate_record("comment", row)
        if any(row[k] != v for k, v in identity.items()):
            raise ContractError("batch_identity_mismatch")
        cid = row["comment_id"]
        if cid in unique and unique[cid] != row:
            raise ContractError("conflicting_duplicate")
        unique[cid] = deepcopy(row)
    by_root, by_user = defaultdict(list), defaultdict(list)
    for row in unique.values():
        by_root[row["root_id"]].append(row)
        by_user[row["author"]["uid"]].append(row)
    roots = sorted(by_root, key=lambda root: order(unique[root]) if root in unique
                   else ("~", int(root)))
    reasons = set(manifest["coverage"]["reasons"])
    threads = []
    payloads = []
    for index, root in enumerate(roots):
        rows = sorted(by_root[root], key=lambda r: (r["kind"] != "root", *order(r)))
        ids = {r["comment_id"] for r in rows}
        missing = {r["parent_id"] for r in rows if r["parent_id"] and r["parent_id"] not in ids}
        if root not in ids:
            missing.add(root)
        unknown = [r["comment_id"] for r in rows if r["kind"] == "reply" and not r["parent_id"]]
        gaps = set()
        if missing:
            gaps.add("missing_parent")
        if unknown:
            gaps.add("unknown_parent")
        if any(r["author"]["uid"] is None for r in rows):
            gaps.add("unknown_author")
        if any(r["content"]["text"] is None for r in rows):
            gaps.add("missing_content")
        if any(parent in unique for parent in missing):
            gaps.add("cross_thread_parent")
        state = thread_states.get(root, {"pagination_status": "not_started", "count": None})
        gaps.update(state.get("reasons", []))
        pagination = state["pagination_status"]
        if pagination != "verified":
            gaps.add("replies_incomplete")
        reasons.update(gaps)
        folder = f"{thread_directory(manifest['schema_version'])}/{index:03d}"
        info = {**identity, "root_id": root,
                "root_author": unique[root]["author"] if root in ids else {"uid": None, "nickname": None},
                "source_title": None, "comment_count": len(rows),
                "reply_count": sum(r["kind"] == "reply" for r in rows),
                "participant_count": len({r["author"]["uid"] for r in rows} - {None}),
                "unknown_author_comment_count": sum(r["author"]["uid"] is None for r in rows),
                "comments_path": folder + "/comments.jsonl",
                "coverage": {"pagination_status": pagination,
                    "context_status": "gaps" if gaps else "no_known_gaps",
                    "source_reported_reply_count": state.get("count"),
                    "missing_parent_ids": sorted(missing, key=int),
                    "unknown_parent_comment_ids": sorted(unknown, key=int), "reasons": sorted(gaps)}}
        threads.append({**source_thread_index.get(root, {}), "root_id": root, "path": folder + "/thread.json"})
        payloads.append((folder + "/thread.json", "thread", info, rows))
    paging = [item[2]["coverage"]["pagination_status"] for item in payloads]
    cov = manifest["coverage"]
    cov["replies_pagination"] = ("verified" if all(p == "verified" for p in paging)
        and (paging or cov["main_pagination"] == "verified") else
        "not_started" if all(p == "not_started" for p in paging) else "partial")
    if cov["main_pagination"] != "verified":
        reasons.add("main_incomplete")
    if cov["replies_pagination"] != "verified":
        reasons.add("replies_incomplete")
    if unclassified:
        reasons.add("unclassified_record")
    if (cov["main_pagination"] != "verified" or cov["replies_pagination"] != "verified"
            or unclassified or "identity_conflict" in reasons):
        cov["status"] = "partial"
    cov["reasons"] = sorted(reasons)
    cov["context_status"] = "gaps" if reasons else "no_known_gaps"
    users = []
    for uid in sorted(by_user, key=lambda uid: (uid is None, int(uid) if uid else 0)):
        rows = sorted(by_user[uid], key=order)
        name = nickname(rows)
        folder = user_directory(manifest["schema_version"]) + "/" + user_folder(uid, name, manifest["schema_version"])
        info = {**identity, "uid": uid, "display_nickname": name,
                "identity_status": "known" if uid else "unknown", "comment_count": len(rows),
                "thread_ids": sorted({r["root_id"] for r in rows}, key=int),
                "comments_path": folder + "/comments.jsonl", "coverage_status": cov["status"],
                "context_status": cov["context_status"], "reasons": cov["reasons"]}
        users.append({**source_user_index.get(uid, {}), "uid": uid, "path": folder + "/user.json"})
        payloads.append((folder + "/user.json", "user", info, rows))
    manifest.update(threads=threads, users=users,
        counts={"root_comments": sum(r["kind"] == "root" for r in unique.values()),
                "replies": sum(r["kind"] == "reply" for r in unique.values()),
                "comments": len(unique), "known_users": len(set(by_user) - {None}),
                "unknown_author_comments": len(by_user.get(None, [])),
                "unclassified_records": len(unclassified)},
        unclassified_path="unclassified.jsonl" if unclassified else None)
    validate_record("manifest", manifest)
    for _, kind, info, _ in payloads:
        validate_record(kind, info)
    for row in unclassified:
        validate_record("unclassified", row)
    documents = {"README.md": "", "manifest.json": manifest}
    for path, _, info, rows in payloads:
        documents[path] = info
        documents[info["comments_path"]] = rows
    if unclassified:
        documents["unclassified.jsonl"] = unclassified
    return ValidatedDataset.from_documents(documents, evidence=evidence)
