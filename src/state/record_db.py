"""One yaver.sqlite file for the small Yaver records.

Jobs, schedules, session binds, issue state, and queue rows share this
file. Indexed columns are for filters. The rest of each record, including
deliveries, is a JSON document. Plans, session logs, and git clones stay
files.

Import of leftover JSON is one-shot. After the meta flag is set, callers
must not scan those folders again.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.logger import logger

# Standard data-dir children share {parent}/yaver.sqlite. A test folder
# with any other name keeps the database inside itself so two stores
# created under the same tmp parent do not share a file by accident.
STORE_DIR_NAMES = frozenset(
    {"jobs", "schedules", "opencode-binds", "queue", "state"}
)

_TABLES = frozenset(
    {"jobs", "schedules", "session_binds", "issue_states", "queue_items"}
)
_DECLS = frozenset({"TEXT NOT NULL DEFAULT ''", "INTEGER"})


def database_path(folder: Path) -> Path:
    folder = Path(folder)
    if folder.name in STORE_DIR_NAMES:
        return folder.parent / "yaver.sqlite"
    return folder / "yaver.sqlite"


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS yaver_meta ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
    )
    conn.commit()
    return conn


def import_done(conn: sqlite3.Connection, flag: str) -> bool:
    row = conn.execute(
        "SELECT value FROM yaver_meta WHERE key = ?",
        (flag,),
    ).fetchone()
    return row is not None and str(row["value"]) == "1"


def mark_imported(conn: sqlite3.Connection, flag: str) -> None:
    conn.execute(
        "INSERT INTO yaver_meta (key, value) VALUES (?, '1') "
        "ON CONFLICT(key) DO UPDATE SET value = '1'",
        (flag,),
    )
    conn.commit()


def dumps(record: Dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False)


def loads(text: Any) -> Optional[Dict[str, Any]]:
    if text is None:
        return None
    raw_text = text if isinstance(text, str) else str(text)
    if not raw_text.strip():
        return None
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError:
        return None
    if isinstance(raw, dict):
        return raw
    return None


def ensure_column(
    conn: sqlite3.Connection, table: str, name: str, decl: str
) -> None:
    """Add a column when an older table is missing it.

    ``table``, ``name``, and ``decl`` are fixed literals from this package.
    """
    if table not in _TABLES or not name.isidentifier() or decl not in _DECLS:
        raise ValueError(f"refusing schema change {table}.{name}")
    cols = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def import_json_once(
    conn: sqlite3.Connection,
    folder: Path,
    *,
    flag: str,
    pattern: str,
    consume: Callable[[Path], str],
) -> int:
    """Import ``pattern`` once, then set ``flag`` so later calls do not glob.

    ``consume`` returns ``ok`` (row saved; this deletes the file), ``bad``
    (corrupt; file stays), or ``skip`` (not our record; file stays).
    A glob OSError leaves the flag unset so the next start can retry.
    The caller holds the lock that guards ``conn``.
    """
    if import_done(conn, flag):
        return 0
    folder = Path(folder)
    if not folder.is_dir():
        mark_imported(conn, flag)
        return 0
    try:
        paths = list(folder.glob(pattern))
    except OSError:
        raise
    imported = 0
    for path in paths:
        outcome = consume(path)
        if outcome != "ok":
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.warning(f"Imported {path.name} but could not remove it: {exc}")
        imported += 1
    mark_imported(conn, flag)
    return imported
