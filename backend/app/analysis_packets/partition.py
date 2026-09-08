"""Deterministic target partitioning; oversize targets remain explicit."""


def partition_targets(targets, packet_factory, limit, pending_ids=()):
    packets, pending, current = [], [], []
    blocked = set(pending_ids)
    for cid in targets:
        if cid in blocked:
            pending.append(
                {
                    "comment_id": cid,
                    "reason": "dependency_missing",
                    "detail": "Required prior result is unavailable.",
                }
            )
            continue
        candidate = packet_factory(tuple(current + [cid]))
        if candidate["chunk"]["estimated_input_tokens"] <= limit:
            current.append(cid)
            continue
        if current:
            packets.append(packet_factory(tuple(current)))
            current = []
        single = packet_factory((cid,))
        if single["chunk"]["estimated_input_tokens"] <= limit:
            current = [cid]
        else:
            pending.append(
                {
                    "comment_id": cid,
                    "reason": "oversize_comment",
                    "detail": "The complete target and required context exceed the limit.",
                }
            )
    if current:
        packets.append(packet_factory(tuple(current)))
    return packets, pending
