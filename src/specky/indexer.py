"""Populates the SQLite index (documents + commits + their FTS5 tables) from what's
actually on disk and in git log. `specky index` does a full rebuild each run rather than
an incremental sync — a repo's specs/ tree and git log are cheap enough to re-walk, and
a full rebuild can't drift from reality the way incremental updates could.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from specky.db import connect


def _domain_for(md_path: Path, specs_root: Path) -> str:
    rel = md_path.relative_to(specs_root)
    return rel.parts[0] if len(rel.parts) > 1 else "root"


def _title_for(content: str, fallback: str) -> str:
    for line in content.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return fallback


def _reset_tables(conn) -> None:
    conn.execute("DELETE FROM documents")
    conn.execute("DELETE FROM documents_fts")
    conn.execute("DELETE FROM commits")
    conn.execute("DELETE FROM commits_fts")


def index_documents(repo_root: Path, conn) -> int:
    specs_root = repo_root / "specs"
    if not specs_root.exists():
        return 0

    count = 0
    for md_path in sorted(specs_root.rglob("*.md")):
        content = md_path.read_text()
        domain = _domain_for(md_path, specs_root)
        title = _title_for(content, md_path.stem)
        rel_path = str(md_path.relative_to(repo_root))
        updated_at = datetime.fromtimestamp(md_path.stat().st_mtime, tz=timezone.utc).isoformat()

        conn.execute(
            "INSERT INTO documents (path, domain, title, content, updated_at) VALUES (?, ?, ?, ?, ?)",
            (rel_path, domain, title, content, updated_at),
        )
        conn.execute(
            "INSERT INTO documents_fts (path, domain, title, content) VALUES (?, ?, ?, ?)",
            (rel_path, domain, title, content),
        )
        count += 1
    return count


def index_commits(repo_root: Path, conn) -> int:
    log = subprocess.run(
        ["git", "log", "--format=%H%x1f%an <%ae>%x1f%aI%x1f%s", "--reverse"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    count = 0
    for line in log.splitlines():
        if not line:
            continue
        sha, author, date, subject = line.split("\x1f", 3)
        conn.execute(
            "INSERT INTO commits (sha, author, date, message) VALUES (?, ?, ?, ?)",
            (sha, author, date, subject),
        )
        row = conn.execute("SELECT summary FROM micro_docs WHERE sha = ?", (sha,)).fetchone()
        summary = row[0] if row else ""
        conn.execute(
            "INSERT INTO commits_fts (sha, message, summary) VALUES (?, ?, ?)",
            (sha, subject, summary),
        )
        count += 1
    return count


def run_index(repo_root: Path) -> tuple[int, int]:
    conn = connect(repo_root)
    try:
        _reset_tables(conn)
        doc_count = index_documents(repo_root, conn)
        commit_count = index_commits(repo_root, conn)
        conn.commit()
        return doc_count, commit_count
    finally:
        conn.close()


def search(repo_root: Path, query: str, limit: int = 10) -> list[dict]:
    conn = connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT path, domain, title, snippet(documents_fts, 3, '>>', '<<', '…', 24) "
            "FROM documents_fts WHERE documents_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
        return [
            {"path": path, "domain": domain, "title": title, "snippet": snippet}
            for path, domain, title, snippet in rows
        ]
    finally:
        conn.close()
