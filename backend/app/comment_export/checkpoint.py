"""Single-process, transactional recovery storage for one collection task."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import checkpoint_layout as layout
from .contract import ContractError, parse_json


class Checkpoint:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        try:
            self._connection.execute('BEGIN IMMEDIATE')
            version = layout.detect_layout(self._connection)
            if version == 1:
                has_state = self._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='state'"
                ).fetchone()
                if has_state:
                    from .publication import lock_owned
                    if not lock_owned(path.parent / '.collect.lock'):
                        raise ContractError('checkpoint_migration_requires_lock')
                layout.initialize_v2(self._connection)
            else:
                try:
                    summary = self._read('progress_summary', None)
                except ContractError:
                    summary = None
                if not layout.summary_valid(summary):
                    layout.rebuild_summary(self._connection)
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            self._connection.close()
            raise

    @contextmanager
    def _transaction(self):
        with self._connection:
            self._connection.execute('BEGIN IMMEDIATE')
            layout.detect_layout(self._connection)
            yield

    def _read(self, key: str, default: dict) -> dict:
        row = self._connection.execute('SELECT value FROM state WHERE key = ?',
                                       (key,)).fetchone()
        return parse_json(row[0]) if row else default

    def _write(self, key: str, value: dict) -> None:
        if key == 'progress':
            layout.store_progress(self._connection, value)
            layout.rebuild_summary(self._connection)
            return
        payload = json.dumps(value, ensure_ascii=True, allow_nan=False)
        self._connection.execute(
            'INSERT INTO state (key, value) VALUES (?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, payload))

    def initialize(self, metadata: dict) -> None:
        with self._transaction():
            existing = self._read('metadata', {})
            if existing:
                if existing.get('video_id') != metadata.get('video_id'):
                    raise ContractError('video_identity_mismatch')
                return
            self._write('metadata', metadata)
            layout.rebuild_summary(self._connection)

    def get_progress(self) -> dict:
        with self._connection:
            self._connection.execute('BEGIN')
            return layout.compose_progress(self._connection)

    def read_request_control(self) -> dict:
        layout.detect_layout(self._connection)
        return layout.validate_control(self._read('request_control', None))

    def reserve_request(self, attempt: dict, max_requests: int) -> dict:
        with self._transaction():
            control = self.read_request_control()
            if type(max_requests) is not int or max_requests < 0:
                raise ContractError('invalid_control_state')
            budget = min(max_requests, control.get('max_requests', max_requests))
            if control.get('requests', 0) >= budget:
                raise ContractError('budget_exhausted')
            attempt = dict(attempt)
            attempt['checkpoint_revision'] = control.get('checkpoint_revision', 0)
            control.update(requests=control.get('requests', 0) + 1,
                           max_requests=budget, attempt=attempt)
            self._write('request_control', control)
        return control

    def _control_progress(self, progress: dict, *, advance: bool = False) -> dict:
        current = self.read_request_control()
        saved = dict(progress)
        saved['checkpoint_revision'] = current.get('checkpoint_revision', 0) + int(advance)
        if 'requests' in current or 'requests' in saved:
            saved['requests'] = max(current.get('requests', 0), saved.get('requests', 0))
        if 'max_requests' in current:
            saved['max_requests'] = min(current['max_requests'], saved.get('max_requests', current['max_requests']))
        return saved

    def set_progress(self, progress: dict) -> None:
        with self._transaction():
            progress = self._control_progress(progress)
            if 'metadata' in progress:
                current = self._read('metadata', {})
                if current and current.get('video_id') != progress['metadata'].get('video_id'):
                    raise ContractError('video_identity_mismatch')
                self._write('metadata', progress['metadata'])
            self._write('progress', progress)

    def save_metadata(self, metadata: dict) -> None:
        with self._transaction():
            existing = self._read('metadata', {})
            if existing and existing.get('video_id') != metadata.get('video_id'):
                raise ContractError('video_identity_mismatch')
            self._write('metadata', metadata)
            layout.rebuild_summary(self._connection)

    def commit_page(self, key: str, comments: list[dict], progress: dict) -> None:
        with self._transaction():
            if self._connection.execute('SELECT 1 FROM pages WHERE key = ?',
                                        (key,)).fetchone():
                return
            self._connection.execute('INSERT INTO pages (key) VALUES (?)', (key,))
            self._write_comments(comments)
            saved = self._publish_progress(progress)
        progress.update({key: value for key, value in saved.items() if key != 'metadata'})

    def _write_comments(self, comments: list[dict]) -> None:
        for comment in comments:
            comment_id = comment.get('comment_id')
            if not isinstance(comment_id, str) or not comment_id:
                raise ContractError('invalid_comment_id')
            row = self._connection.execute(
                'SELECT value FROM comments WHERE comment_id = ?', (comment_id,)).fetchone()
            if row:
                previous = parse_json(row[0])
                if (previous.get('root_id') != comment.get('root_id')
                        or previous.get('author', {}).get('uid') !=
                        comment.get('author', {}).get('uid')):
                    raise ContractError('identity_conflict')
            self._connection.execute(
                'INSERT INTO comments (comment_id, value) VALUES (?, ?) '
                'ON CONFLICT(comment_id) DO UPDATE SET value = excluded.value',
                (comment_id, json.dumps(comment, ensure_ascii=True, allow_nan=False)))

    def _publish_progress(self, progress: dict, *, advance: bool = True) -> dict:
        if 'metadata' in progress:
            current = self._read('metadata', {})
            if current and current.get('video_id') != progress['metadata'].get('video_id'):
                raise ContractError('video_identity_mismatch')
            self._write('metadata', progress['metadata'])
        saved = self._control_progress(progress, advance=advance)
        self._write('progress', saved)
        return saved

    def _save_tail_control(self, progress: dict) -> dict:
        saved = self._control_progress(progress)
        layout.store_progress(self._connection, saved)
        summary = self._read('progress_summary', None)
        if layout.summary_valid(summary):
            summary['phase'] = layout.phase(saved)
            self._write('progress_summary', summary)
        else:
            layout.rebuild_summary(self._connection)
        return saved

    def _tail_status(self, root_id: str, attempt: int) -> str:
        row = self._connection.execute(
            'SELECT attempt, status FROM tail_attempts WHERE root_id = ?',
            (root_id,)).fetchone()
        if not row or row[0] != attempt:
            raise ContractError('invalid_tail_attempt')
        return row[1]

    def _require_active_tail(self, root_id: str, attempt: int) -> None:
        if self._tail_status(root_id, attempt) != 'active':
            raise ContractError('invalid_tail_attempt')

    def begin_tail(self, root_id: str, plan: dict, progress: dict) -> int:
        with self._transaction():
            row = self._connection.execute(
                'SELECT attempt FROM tail_attempts WHERE root_id = ?', (root_id,)).fetchone()
            attempt = row[0] + 1 if row else 1
            self._connection.execute(
                'INSERT INTO tail_attempts (root_id, attempt, status, plan_json) '
                "VALUES (?, ?, 'active', ?) ON CONFLICT(root_id) DO UPDATE SET "
                "attempt = excluded.attempt, status = 'active', plan_json = excluded.plan_json",
                (root_id, attempt, json.dumps(plan, ensure_ascii=True, allow_nan=False)))
            self._connection.execute('DELETE FROM tail_pages WHERE root_id = ?', (root_id,))
            saved = self._save_tail_control(progress)
        progress.update({key: value for key, value in saved.items() if key != 'metadata'})
        return attempt

    def stage_tail_page(self, root_id: str, attempt: int, page: int,
                        payload: dict, progress: dict) -> None:
        with self._transaction():
            self._require_active_tail(root_id, attempt)
            encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True)
            existing = self._connection.execute(
                'SELECT payload FROM tail_pages WHERE root_id = ? AND attempt = ? AND page = ?',
                (root_id, attempt, page)).fetchone()
            if existing and existing[0] != encoded:
                raise ContractError('tail_page_conflict')
            if not existing:
                self._connection.execute(
                    'INSERT INTO tail_pages (root_id, attempt, page, payload) VALUES (?, ?, ?, ?)',
                    (root_id, attempt, page, encoded))
            saved = self._save_tail_control(progress)
        progress.update({key: value for key, value in saved.items() if key != 'metadata'})

    def read_tail_pages(self, root_id: str, attempt: int) -> list[dict]:
        self._require_active_tail(root_id, attempt)
        return [parse_json(row[0]) for row in self._connection.execute(
            'SELECT payload FROM tail_pages WHERE root_id = ? AND attempt = ? ORDER BY page',
            (root_id, attempt))]

    def promote_tail(self, root_id: str, attempt: int, rows: list[dict], progress: dict) -> None:
        with self._transaction():
            if self._tail_status(root_id, attempt) == 'promoted':
                return
            self._require_active_tail(root_id, attempt)
            self._write_comments(rows)
            saved = self._publish_progress(progress)
            self._connection.execute(
                "UPDATE tail_attempts SET status = 'promoted' WHERE root_id = ? AND attempt = ?",
                (root_id, attempt))
        progress.update({key: value for key, value in saved.items() if key != 'metadata'})

    def abandon_tail(self, root_id: str, attempt: int, progress: dict, *,
                     confirmed_progress: dict | None = None) -> None:
        """Persist an optional confirmed fallback decision together with abandonment.

        confirmed_progress must exclude unvalidated candidate observations. Metadata
        objects held by the caller stay alive; only non-metadata control is synced.
        """
        with self._transaction():
            self._require_active_tail(root_id, attempt)
            self._connection.execute(
                "UPDATE tail_attempts SET status = 'abandoned' WHERE root_id = ? AND attempt = ?",
                (root_id, attempt))
            saved = self._save_tail_control(progress)
            if confirmed_progress is not None:
                # This second write remains inside the transaction and merges the
                # strictest budget from both caller snapshots via _control_progress.
                saved = self._publish_progress(confirmed_progress, advance=False)
        progress.update({key: value for key, value in saved.items() if key != 'metadata'})

    def freeze(self) -> tuple[list[dict], dict]:
        # One read transaction binds metadata and all records to the same database snapshot.
        self._connection.execute('BEGIN')
        try:
            metadata = self._read('metadata', {'coverage': {'status': 'partial'}})
            records = [parse_json(row[0]) for row in self._connection.execute(
                'SELECT value FROM comments ORDER BY rowid')]
            self._connection.commit()
            return records, metadata
        except BaseException:
            self._connection.rollback()
            raise

    def close(self) -> None:
        self._connection.close()


def read_control_snapshot(path: Path) -> dict:
    path = Path(path)
    database = path if path.name.endswith(".sqlite3") else path / "work.sqlite3"
    if not database.is_file():
        raise ContractError("task_not_found")
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ContractError("invalid_control_state") from exc
    try:
        connection.execute("BEGIN")
        metadata = layout.read(connection, "metadata", {})
        return {"progress": layout.compose_progress(connection), "metadata": metadata,
                "comment_count": connection.execute("SELECT count(*) FROM comments").fetchone()[0]}
    except sqlite3.Error as exc:
        raise ContractError("invalid_control_state") from exc
    finally:
        connection.close()


def read_checkpoint(path: Path) -> tuple[list[dict], dict, dict]:
    """Read records and control state from one immutable SQLite snapshot."""
    path = Path(path)
    database = path if path.name.endswith(".sqlite3") else path / "work.sqlite3"
    if not database.is_file():
        raise ContractError("task_not_found")
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    except (OSError, sqlite3.Error) as exc:
        raise ContractError("invalid_checkpoint") from exc
    try:
        connection.execute("BEGIN")
        metadata = layout.read(connection, "metadata", {})
        records = [
            parse_json(row[0])
            for row in connection.execute("SELECT value FROM comments ORDER BY rowid")
        ]
        progress = layout.compose_progress(connection)
        connection.commit()
        return records, metadata, progress
    except ContractError:
        connection.rollback()
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        connection.rollback()
        raise ContractError("invalid_checkpoint") from exc
    finally:
        connection.close()


def read_progress_summary(path: Path) -> dict:
    path = Path(path)
    database = path if path.name.endswith('.sqlite3') else path / 'work.sqlite3'
    if not database.is_file():
        raise ContractError('task_not_found')
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    except (OSError, sqlite3.Error) as exc:
        raise ContractError('invalid_control_state') from exc
    try:
        connection.execute('BEGIN')
        if layout.detect_layout(connection) == 1:
            progress = layout.read(connection, 'progress', {})
            if not isinstance(progress, dict):
                raise ContractError('invalid_control_state')
            control = {k: progress[k] for k in layout.CONTROL_KEYS if k in progress}
            try:
                metadata = layout.read(connection, 'metadata', {})
                threads = metadata.get('_threads', {})
                summary = {
                    'comments': connection.execute('SELECT count(*) FROM comments').fetchone()[0],
                    'threads': len(threads),
                    'verified_threads': sum(s.get('reply_check_state') == 'complete'
                                            for s in threads.values()),
                    'unavailable_threads': sum(bool(s.get('unavailable'))
                                               for s in threads.values()),
                    'phase': layout.phase(progress),
                }
            except (ContractError, AttributeError, TypeError):
                summary = None
        else:
            control = layout.read(connection, 'request_control', None)
            try:
                summary = layout.read(connection, 'progress_summary', None)
            except ContractError:
                summary = None
            if not layout.summary_valid(summary):
                summary = None
        return {'request_control': layout.validate_control(control), 'summary': summary}
    except sqlite3.Error as exc:
        raise ContractError('invalid_control_state') from exc
    finally:
        connection.close()
