"""Per-commit micro-doc generation, called by the git post-commit hook.

This is deliberately a real git hook rather than an agent-specific one (Claude Code
PostToolUse, opencode tool.execute.after, Kiro agent hooks) — it must fire on every
commit regardless of which agent (or no agent) made it. Each commit gets a real
markdown file under specs/history/, plus a mirrored row in the micro_docs sqlite
table for Phase 3's indexer to pick up.
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from specky.ai_provider import ConfigError, Provider, load_provider_from_toml
from specky.db import index_db_path, repo_root as _repo_root

POST_COMMIT_HOOK = """#!/bin/sh
# Installed by `specky install-git-hook`. Records a short AI micro-doc for this commit.
command -v specky >/dev/null 2>&1 && specky commit-doc || true
"""

HOOK_MARKER = "specky commit-doc"


@dataclass
class Commit:
    sha: str
    author: str
    date: str
    message: str
    diff: str


def _commit_info(rev: str = "HEAD") -> Commit:
    sha, author, date, message = subprocess.run(
        ["git", "log", "-1", "--format=%H%x1f%an <%ae>%x1f%aI%x1f%B", rev],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\x1f", 3)
    diff = subprocess.run(
        ["git", "show", "--format=", sha], capture_output=True, text=True, check=True
    ).stdout
    return Commit(sha=sha, author=author, date=date, message=message.strip(), diff=diff)


def generate_micro_doc(commit: Commit, provider: Provider) -> str:
    prompt = (
        "Summarize what changed and why, in one short paragraph, for a commit history reader.\n\n"
        f"Commit message:\n{commit.message}\n\nDiff (may be truncated):\n{commit.diff[:8000]}"
    )
    return provider.generate(prompt).strip()


def write_history_file(repo_root: Path, commit: Commit, summary: str) -> Path:
    history_dir = repo_root / "specs" / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    path = history_dir / f"{commit.sha[:8]}.md"
    path.write_text(
        f"# Commit {commit.sha[:8]}\n\n"
        f"- **Date:** {commit.date}\n"
        f"- **Author:** {commit.author}\n"
        f"- **Message:** {commit.message.splitlines()[0]}\n\n"
        f"{summary}\n"
    )
    return path


def record_micro_doc(db_path: Path, commit: Commit, summary: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS micro_docs ("
            "sha TEXT PRIMARY KEY, summary TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO micro_docs (sha, summary, created_at) VALUES (?, ?, ?)",
            (commit.sha, summary, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def missing_shas(repo_root: Path) -> list[str]:
    """Every commit sha in HEAD's history that doesn't yet have a specs/history/<sha8>.md."""
    history_dir = repo_root / "specs" / "history"
    done = {p.stem for p in history_dir.glob("*.md")} if history_dir.exists() else set()
    all_shas = subprocess.run(
        ["git", "rev-list", "--reverse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.split()
    return [sha for sha in all_shas if sha[:8] not in done]


def _sync_one(repo_root: Path, commit: Commit, provider: Provider) -> list[Path]:
    """History log entry (changelog trail) + feature/workflow reference doc, if this commit
    affects one. The specs/<domain>/<topic>.md docs are the reference; specs/history/ is just
    the supplementary per-commit trail alongside them."""
    from specky.generator import sync_feature_doc

    summary = generate_micro_doc(commit, provider)
    history_path = write_history_file(repo_root, commit, summary)
    record_micro_doc(index_db_path(repo_root), commit, summary)
    print(f"specky commit-doc: wrote {history_path}")
    written = [history_path]

    feature_doc_path = sync_feature_doc(repo_root, commit, provider)
    if feature_doc_path:
        print(f"specky commit-doc: updated {feature_doc_path}")
        written.append(feature_doc_path)
    return written


def sync() -> list[Path]:
    """Generate a micro-doc + feature/workflow doc update for every commit that doesn't have a
    history entry yet. Idempotent for the history log — safe to re-run any time (e.g. after
    installing specky on a repo with existing history, or after a commit the post-commit hook
    missed because `specky` wasn't on PATH yet). Feature docs may be updated again on a re-sync
    if a later run reclassifies the same commit differently; the history log is what gates which
    commits get (re-)processed at all."""
    repo_root = _repo_root()
    provider = load_provider_from_toml(repo_root / "specky.toml")  # let ConfigError surface

    written = []
    for sha in missing_shas(repo_root):
        written += _sync_one(repo_root, _commit_info(sha), provider)
    return written


def main() -> None:
    repo_root = _repo_root()
    config_path = repo_root / "specky.toml"
    try:
        provider = load_provider_from_toml(config_path)
    except ConfigError as exc:
        print(f"specky commit-doc: skipping ({exc})")
        return

    _sync_one(repo_root, _commit_info("HEAD"), provider)


def install_git_hook() -> Path:
    repo_root = _repo_root()
    hook_path = repo_root / ".git" / "hooks" / "post-commit"
    if hook_path.exists() and HOOK_MARKER not in hook_path.read_text():
        raise RuntimeError(
            f"{hook_path} already exists and wasn't installed by specky — not overwriting it"
        )
    hook_path.write_text(POST_COMMIT_HOOK)
    hook_path.chmod(0o755)
    return hook_path
