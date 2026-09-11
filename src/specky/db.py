"""SQLite schema shared by the indexer, search, and (later) the chat companion.

One database per repo, at .specky/index.db. Tables: documents/commits mirror what's on
disk and in git log; micro_docs is written incrementally by commit_doc.py. FTS5 virtual
tables are kept in sync by indexer.py's full-rebuild pass, not by triggers, since a
`specky index` run is cheap enough to just redo from scratch each time.
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
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    path, domain, title, content
);

CREATE TABLE IF NOT EXISTS commits (
    sha TEXT PRIMARY KEY,
    author TEXT NOT NULL,
    date TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS commits_fts USING fts5(
    sha, message, summary
);

CREATE TABLE IF NOT EXISTS micro_docs (
    sha TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    )
    return Path(result.stdout.strip())


def index_db_path(repo_root: Path) -> Path:
    db_dir = repo_root / ".specky"
    db_dir.mkdir(exist_ok=True)
    return db_dir / "index.db"


def connect(repo_root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(index_db_path(repo_root))
    conn.executescript(SCHEMA_SQL)
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
