"""Versioned checkpoint state, with canonical metadata and compact control."""
import json
import sqlite3
from typing import Any

from .contract import ContractError, parse_json

CONTROL_KEYS = ('requests', 'max_requests', 'checkpoint_revision', 'attempt')


def read(connection: sqlite3.Connection, key: str, default: Any) -> Any:
    row = connection.execute('SELECT value FROM state WHERE key = ?', (key,)).fetchone()
    return parse_json(row[0]) if row else default


def write(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        'INSERT INTO state (key,value) VALUES (?,?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (key, json.dumps(value, ensure_ascii=True, allow_nan=False)))


def detect_layout(connection: sqlite3.Connection) -> int:
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables:
        return 1
    if 'state' not in tables:
        raise ContractError('invalid_checkpoint')
    missing = object()
    version = read(connection, 'checkpoint_format', missing)
    if version is missing:
        if not {'pages', 'comments'}.issubset(tables):
            raise ContractError('invalid_checkpoint')
        return 1
    if type(version) is not int or version != 2:
        raise ContractError('unsupported_checkpoint_format')
    return 2


def validate_control(control: Any) -> dict:
    if not isinstance(control, dict):
        raise ContractError('invalid_control_state')
    for key in CONTROL_KEYS[:3]:
        if key in control and (type(control[key]) is not int or control[key] < 0):
            raise ContractError('invalid_control_state')
    if 'attempt' in control and not isinstance(control['attempt'], dict):
        raise ContractError('invalid_control_state')
    return control


def phase(progress: dict) -> str:
    return 'import' if progress.get('finished') else 'replies' if progress.get('main_done') else 'main'


def summary_valid(summary: Any) -> bool:
    return (isinstance(summary, dict) and summary.get('phase') in {'main', 'replies', 'import'}
            and all(type(summary.get(key)) is int and summary[key] >= 0 for key in
                    ('comments', 'threads', 'verified_threads', 'unavailable_threads')))


def rebuild_summary(connection: sqlite3.Connection) -> dict:
    metadata = read(connection, 'metadata', {})
    threads = metadata.get('_threads', {})
    summary = {
        'comments': connection.execute('SELECT count(*) FROM comments').fetchone()[0],
        'threads': len(threads),
        'verified_threads': sum(s.get('reply_check_state') == 'complete' for s in threads.values()),
        'unavailable_threads': sum(bool(s.get('unavailable')) for s in threads.values()),
        'phase': phase(read(connection, 'progress', {})),
    }
    write(connection, 'progress_summary', summary)
    return summary


def store_progress(connection: sqlite3.Connection, progress: dict) -> None:
    write(connection, 'progress', {k: v for k, v in progress.items()
                                  if k not in CONTROL_KEYS and k != 'metadata'})
    write(connection, 'request_control', validate_control(
        {k: progress[k] for k in CONTROL_KEYS if k in progress}))
    write(connection, 'progress_metadata_attached', 'metadata' in progress)


def compose_progress(connection: sqlite3.Connection) -> dict:
    progress = read(connection, 'progress', {})
    if detect_layout(connection) == 1:
        return progress
    if not isinstance(progress, dict):
        raise ContractError('invalid_control_state')
    progress.update(validate_control(read(connection, 'request_control', None)))
    if read(connection, 'progress_metadata_attached', False):
        progress['metadata'] = read(connection, 'metadata', {})
    return progress


def initialize_v2(connection: sqlite3.Connection) -> None:
    if detect_layout(connection) == 2:
        return
    for ddl in (
        'CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY,value TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS pages (key TEXT PRIMARY KEY)',
        'CREATE TABLE IF NOT EXISTS comments (comment_id TEXT PRIMARY KEY,value TEXT NOT NULL)',
        ('CREATE TABLE IF NOT EXISTS tail_attempts (root_id TEXT PRIMARY KEY, '
        'attempt INTEGER NOT NULL,status TEXT NOT NULL,plan_json TEXT NOT NULL)'),
        ('CREATE TABLE IF NOT EXISTS tail_pages (root_id TEXT,attempt INTEGER,page INTEGER,'
        'payload TEXT NOT NULL,PRIMARY KEY(root_id,attempt,page))'),
    ):
        connection.execute(ddl)
    migrate_v1(connection)


def migrate_v1(connection: sqlite3.Connection) -> None:
    if detect_layout(connection) == 2:
        return
    metadata = read(connection, 'metadata', {})
    progress = read(connection, 'progress', {})
    if not isinstance(metadata, dict) or not isinstance(progress, dict):
        raise ContractError('invalid_checkpoint')
    if 'metadata' in progress:
        embedded = progress['metadata']
        if not isinstance(embedded, dict) or not metadata:
            raise ContractError('invalid_checkpoint')
        if embedded.get('video_id') != metadata.get('video_id'):
            raise ContractError('video_identity_mismatch')
    store_progress(connection, progress)
    rebuild_summary(connection)
    write(connection, 'checkpoint_format', 2)
