"""One joined projection for all stored comment readers."""

from sqlalchemy import and_, select
from sqlalchemy.sql import Select

from .schema import comment_payloads, comments


def comment_select() -> Select:
    c, p = comments.c, comment_payloads.c
    return select(comments, p.content, p.extra_fields).select_from(comments.join(
        comment_payloads,
        and_(c.video_id == p.video_id, c.comment_id == p.comment_id, c.payload_id == p.payload_id),
    ))
