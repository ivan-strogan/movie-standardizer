"""SQLite cache for AI torrent classifications and sizes.

DB location: OUTPUT_DIR/classifications.db

If OUTPUT_DIR is not accessible (drive not mounted), operations raise
so the caller fails loudly rather than silently using stale data.

Schema:
    classifications(name TEXT PK, category TEXT, manual INTEGER, size_gb REAL)

manual=1 means the user set this category manually -- AI re-runs never
overwrite it.  manual=0 means the AI set it and a future re-run can
refresh it.
"""
from __future__ import annotations

import sqlite3

from .. import config

_DB_PATH = config.OUTPUT_DIR / "classifications.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    if not config.OUTPUT_DIR.is_dir():
        raise RuntimeError(
            f"OUTPUT_DIR not accessible: {config.OUTPUT_DIR} -- is the drive mounted?"
        )
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS classifications (
                name     TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                manual   INTEGER NOT NULL DEFAULT 0,
                size_gb  REAL
            )
        """)
        # Migrate existing DBs that pre-date the size_gb column
        try:
            conn.execute("ALTER TABLE classifications ADD COLUMN size_gb REAL")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()
    finally:
        conn.close()


def get_all() -> dict[str, dict]:
    """Return {name: {category, manual, size_gb}} for every cached entry."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT name, category, manual, size_gb FROM classifications"
        ).fetchall()
        return {
            row["name"]: {
                "category": row["category"],
                "manual":   bool(row["manual"]),
                "size_gb":  row["size_gb"],
            }
            for row in rows
        }
    finally:
        conn.close()


def set_size(name: str, size_gb: float) -> None:
    """Update the cached size for an entry that already exists in the DB.
    If the name is not yet in the DB, insert it with category='unknown'."""
    conn = _connect()
    try:
        conn.execute("""
            INSERT INTO classifications (name, category, size_gb)
            VALUES (?, 'unknown', ?)
            ON CONFLICT(name) DO UPDATE SET size_gb = excluded.size_gb
        """, (name, size_gb))
        conn.commit()
    finally:
        conn.close()


def set_category(name: str, category: str, manual: bool = False) -> None:
    """Upsert one entry. manual=True entries are never overwritten by AI."""
    conn = _connect()
    try:
        conn.execute(_UPSERT_SQL, (name, category, int(manual)))
        conn.commit()
    finally:
        conn.close()


def set_many(entries: dict[str, str], manual: bool = False) -> None:
    """Batch upsert {name: category}. Existing manual entries are not touched."""
    if not entries:
        return
    conn = _connect()
    try:
        conn.executemany(
            _UPSERT_SQL,
            [(name, cat, int(manual)) for name, cat in entries.items()],
        )
        conn.commit()
    finally:
        conn.close()


def reset_auto() -> int:
    """Delete all auto-classified entries (manual=0). Returns count deleted.

    Manual overrides (manual=1) are preserved so user corrections survive the reset.
    """
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM classifications WHERE manual = 0")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def reset_auto() -> int:
    """Delete all auto-classified entries (manual=0). Returns count deleted.

    Manual overrides (manual=1) are preserved so user corrections survive the reset.
    """
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM classifications WHERE manual = 0")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def reset_all() -> int:
    """Truncate the entire classifications table. Returns count deleted."""
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM classifications")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# Upsert rule:
#   - If the existing row is manual=1, keep its category (user override wins).
#   - If the incoming row is manual=1, store it and set manual=1 (user is overriding).
#   - Otherwise just update category.
_UPSERT_SQL = """
    INSERT INTO classifications (name, category, manual)
    VALUES (?, ?, ?)
    ON CONFLICT(name) DO UPDATE SET
        category = CASE
            WHEN classifications.manual = 1 AND excluded.manual = 0
                THEN classifications.category
            ELSE excluded.category
        END,
        manual = MAX(classifications.manual, excluded.manual)
"""
