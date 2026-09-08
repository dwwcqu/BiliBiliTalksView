"""Select reply-related context without guessing missing relationships."""

from copy import deepcopy

from .source import SourceBundle


def select_context(bundle: SourceBundle, target_ids: tuple[str, ...]):
    rows = bundle._data["comments"]
    targets = set(target_ids)
    if len(targets) != len(target_ids) or not targets <= rows.keys():
        raise ValueError("invalid_targets")
    reasons: dict[str, set[str]] = {cid: {"target"} for cid in targets}
    gaps = []

    def gap(kind, ids, roots):
        item = {
            "kind": kind,
            "comment_ids": sorted(set(ids), key=int),
            "root_ids": sorted(set(roots), key=int),
            "detail": kind,
        }
        if item not in gaps:
            gaps.append(item)

    def include(cid, reason):
        reasons.setdefault(cid, set()).add(reason)

    for cid in target_ids:
        row = rows[cid]
        root = row["root_id"]
        if root in rows:
            include(root, "root")
        else:
            gap("missing_root", [root], [root])
        visited = {cid}
        current = row
        while current["parent_id"] is not None:
            parent = current["parent_id"]
            if parent in visited:
                gap("reply_cycle", [parent], [root])
                break
            visited.add(parent)
            if parent not in rows:
                gap("missing_parent", [parent], [root])
                break
            if rows[parent]["root_id"] != root:
                gap("cross_thread_parent", [parent], [root])
                break
            include(parent, "ancestor")
            current = rows[parent]
        if current["kind"] == "reply" and current["parent_id"] is None:
            gap("unknown_parent", [current["comment_id"]], [root])
        for reply in rows.values():
            if reply["parent_id"] == cid and reply["root_id"] == root:
                include(reply["comment_id"], "direct_reply")
    result = []
    for cid, why in reasons.items():
        row = rows[cid]
        if row["content"]["text"] is None:
            gap("missing_text", [cid], [row["root_id"]])
        if row["content"]["images"] or row["content"]["emotes"]:
            gap("media_unread", [cid], [row["root_id"]])
        result.append(
            {
                "comment_id": cid,
                "root_id": row["root_id"],
                "parent_id": row["parent_id"],
                "kind": row["kind"],
                "author_uid": row["author"]["uid"],
                "reply_relation": deepcopy(row["reply_relation"]),
                "content": deepcopy(row["content"]),
                "created_at": row["created_at"],
                "collected_at": row["collected_at"],
                "input_role": "target" if cid in targets else "context",
                "context_reasons": sorted(why),
            }
        )
    result.sort(
        key=lambda r: (
            int(r["root_id"]),
            r["kind"] != "root",
            r["created_at"] is None,
            r["created_at"] or "",
            int(r["comment_id"]),
        )
    )
    return result, gaps
