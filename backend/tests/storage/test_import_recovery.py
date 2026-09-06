import os
import subprocess
import sys
from textwrap import dedent

from sqlalchemy import event, func, select, text

from app.comment_export.export import build_batch
from app.storage.frozen import freeze_batch
from app.storage.importer import import_batch
from app.storage.schema import comment_payloads, comments, import_receipts


def test_killed_importer_releases_ownership_and_reimports(conn, frozen_case, tmp_path):
    source = build_batch(*frozen_case, tmp_path / "batch")
    with conn.begin():
        schema = conn.scalar(text("select current_schema()"))
    env = dict(
        os.environ,
        TEST_IMPORT_SCHEMA=schema,
        TEST_IMPORT_SOURCE=str(source),
        TEST_IMPORT_WORK=str(tmp_path / "child-work"),
    )
    script = dedent("""
        import os
        from pathlib import Path
        from sqlalchemy import create_engine
        from app.storage.frozen import freeze_batch
        from app.storage import importer
        original = importer._load
        def die_after_write(conn, frozen, state_id):
            original(conn, frozen, state_id)
            os._exit(19)
        importer._load = die_after_write
        engine = create_engine(os.environ['TEST_DATABASE_URL'], hide_parameters=True,
            connect_args={'options': '-csearch_path=' + os.environ['TEST_IMPORT_SCHEMA']})
        with engine.connect() as connection:
            with freeze_batch(Path(os.environ['TEST_IMPORT_SOURCE']), Path(os.environ['TEST_IMPORT_WORK'])) as frozen:
                importer.import_batch(connection, frozen)
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, timeout=20, check=False
    )
    assert result.returncode == 19
    with conn.begin():
        assert conn.scalar(select(import_receipts.c.status)) == "loading"
        assert conn.scalar(select(func.count()).select_from(comments)) == 3
    inserts = []

    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        if "INSERT INTO comment_payloads" in statement:
            inserts.append(statement)

    event.listen(conn, "before_cursor_execute", observe)
    try:
        with freeze_batch(source, tmp_path / "parent-work") as frozen:
            result = import_batch(conn, frozen, publish=True)
    finally:
        event.remove(conn, "before_cursor_execute", observe)
    assert inserts == []
    assert result["published"]
    with conn.begin():
        assert conn.scalar(select(func.count()).select_from(comments)) == 3
        assert conn.scalar(select(import_receipts.c.status)) == "published"
        assert conn.scalar(select(func.count()).select_from(comment_payloads)) == 3
