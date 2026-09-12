"""SQLite schema shared by the indexer, search, and (later) the chat companion.

One database per repo, at .specky/index.db. Tables: documents/commits mirror what's on
disk and in git log; micro_docs and commit_links are written incrementally by
commit_doc.py. FTS5 virtual tables are kept in sync by indexer.py's full-rebuild pass, not
by triggers, since a `specky index` run is cheap enough to just redo from scratch each time.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    path TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    doc_type TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    related TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    path, domain, title, content, tags
);

CREATE TABLE IF NOT EXISTS commits (
    sha TEXT PRIMARY KEY,
    author TEXT NOT NULL,
    date TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS commits_fts USING fts5(
    sha, message, summary, tags
);

CREATE TABLE IF NOT EXISTS micro_docs (
    sha TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commit_links (
    sha TEXT NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (sha, path)
);
"""

# Columns added to `documents`/`commits` after their initial release. connect() adds any
# that are missing from an existing .specky/index.db via ALTER TABLE, so repos indexed
# before this change don't need to delete their db file.
_ADDED_COLUMNS = {
    "documents": [
        ("doc_type", "TEXT NOT NULL DEFAULT ''"),
        ("tags", "TEXT NOT NULL DEFAULT ''"),
        ("related", "TEXT NOT NULL DEFAULT ''"),
    ],
}

# FTS5 virtual tables can't gain columns via ALTER TABLE. Their content is always fully
# rebuilt by the next `specky index` run (see indexer.py), so it's safe to drop and
# recreate one whose column set is stale.
_FTS_TABLES = {
    "documents_fts": "path, domain, title, content, tags",
    "commits_fts": "sha, message, summary, tags",
}


def repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    )
    return Path(result.stdout.strip())


def index_db_path(repo_root: Path) -> Path:
    db_dir = repo_root / ".specky"
    db_dir.mkdir(exist_ok=True)
    return db_dir / "index.db"


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    for table, columns in _FTS_TABLES.items():
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        current = [row[1] for row in info]
        expected = [c.strip() for c in columns.split(",")]
        if current and current != expected:
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"CREATE VIRTUAL TABLE {table} USING fts5({columns})")


def connect(repo_root: Path) -> sqlite3.Connection:
    """Open (creating if needed) this repo's index, with the schema migrated forward.

    Every writer and reader in specky goes through here, and they genuinely overlap: the
    post-commit hook writes `micro_docs`/`commit_links` while a `specky serve` process is
    answering questions out of the same file, and `specky index` rewrites both FTS tables
    wholesale. Under the default rollback journal a reader blocks the writer and the loser gets
    `database is locked` immediately, which surfaces as a failed hook or a 500 from the chat
    server for no reason a user can act on. Hence:

    - WAL, so readers never block the writer and vice versa. Set per-database and persistent, so
      this is a one-time switch; on a filesystem that can't support it (some network mounts, no
      shared memory) SQLite just reports back the mode it kept, which is why the result is
      ignored rather than checked.
    - `busy_timeout`, so two *writers* — a hook firing mid-`specky index` — wait their turn
      instead of failing at once.
    - `synchronous=NORMAL`, safe under WAL: a crash can lose the last transactions but can't
      corrupt the file, and every table here is derived data that `specky index` rebuilds.
    """
    conn = sqlite3.connect(index_db_path(repo_root))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA_SQL)
    _migrate(conn)
    return conn


def fts_match_query(text: str) -> str | None:
    """Build a safe FTS5 MATCH expression from free-form text, or None if there's nothing
    to search for. Quotes every token as a literal phrase so stray FTS5 operator syntax in
    the input (".", "-", "*", NEAR, AND...) is treated as plain text rather than a query
    operator, and ORs them together so a multi-word query behaves like normal keyword search
    (broader recall) instead of requiring every word to appear."""
    tokens = re.findall(r"\w+", text)
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)
