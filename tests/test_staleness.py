"""Staleness: is a doc still true, judged from git dates alone.

Every commit here is dated explicitly, because that's the whole input — the pass compares the
committer date of a doc's last change against the newest committer date among the code files
`doc_files` says it covers. The coverage itself is built the way the post-commit hook builds it (a
code commit, then a `docs: sync specky docs [skip specky]` commit naming it), so these tests
exercise the same map `specky check` and the HTML viewer read.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from specky import gitlog, staleness
from specky.check import run_check
from specky.db import connect
from specky.html_render import render_site
from specky.indexer import run_index
from specky.staleness import days_behind, index_staleness

from conftest import git

DOC = "specs/billing/refund-flow.md"
DOC_BODY = "# Billing — Refund Flow\n\nHow a refund runs.\n"


def _commit(repo: Path, message: str, files: dict[str, str], days_ago: int = 0) -> str:
    """A commit dated `days_ago` days back on *both* sides — staleness reads `%cI`, and a test
    that only set the author date would pass while measuring nothing."""
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=repo, env=env, check=True, capture_output=True
    )
    return git(repo, "rev-parse", "HEAD").strip()


def _document(repo: Path, sha: str, days_ago: int, docs: dict[str, str] | None = None) -> str:
    files = {f"specs/history/{sha[:8]}.md": f"---\nsha: {sha}\n---\n\n# Commit {sha[:8]}\n"}
    files.update(docs or {})
    return _commit(repo, "docs: sync specky docs [skip specky]", files, days_ago=days_ago)


def _link(repo: Path, doc_days_ago: int) -> None:
    """Two documented commits pairing `src/app.py` with DOC, both dated `doc_days_ago` back.

    Two, because that's what `check`'s recurrence threshold asks for — and the doc body has to
    change each time, since git records a commit as touching a file only if its content moved.
    """
    for i in range(2):
        sha = _commit(repo, f"work {i}", {"src/app.py": f"v{i}\n"}, days_ago=doc_days_ago)
        _document(repo, sha, doc_days_ago, docs={DOC: f"{DOC_BODY}\nRevision {i}.\n"})


def _row(repo: Path, doc_path: str = DOC) -> tuple[str, str]:
    conn = connect(repo)
    try:
        return conn.execute(
            "SELECT stale_since, last_code_change FROM documents WHERE path = ?", (doc_path,)
        ).fetchone()
    finally:
        conn.close()


@pytest.fixture
def stale_repo(tmp_repo) -> Path:
    """DOC last changed 60 days ago; the code it covers changed 6 days ago."""
    _link(tmp_repo, doc_days_ago=60)
    _commit(tmp_repo, "change the code, leave the doc", {"src/app.py": "v9\n"}, days_ago=6)
    run_index(tmp_repo)
    return tmp_repo


def test_a_doc_its_code_outran_is_stale(stale_repo):
    stale_since, last_code = _row(stale_repo)
    assert stale_since and last_code
    # Not an exact figure: the dates are relative to the moment the test ran, so the arithmetic is
    # 54 days give or take the rounding of a whole-day truncation.
    assert 53 <= days_behind(stale_since, last_code) <= 55


def test_a_doc_updated_after_its_code_is_fresh(stale_repo):
    _commit(stale_repo, "docs: catch up", {DOC: f"{DOC_BODY}\nRewritten.\n"})
    run_index(stale_repo)
    assert _row(stale_repo) == ("", "")


def test_a_doc_no_file_map_mentions_is_neither_stale_nor_fresh(stale_repo, write_doc):
    """Nothing is known about a doc with no coverage, and inventing a verdict for it would be
    worse than leaving the badge off."""
    write_doc("billing/glossary-of-fees.md", "# Fees\n\nWords only.\n")
    run_index(stale_repo)
    assert _row(stale_repo, "specs/billing/glossary-of-fees.md") == ("", "")


def test_a_doc_its_code_outran_by_less_than_the_threshold_is_fresh(tmp_repo):
    _link(tmp_repo, doc_days_ago=30)
    _commit(tmp_repo, "small follow-up", {"src/app.py": "v9\n"}, days_ago=27)
    run_index(tmp_repo)
    assert _row(tmp_repo) == ("", "")


def test_the_threshold_comes_from_the_check_table(tmp_repo):
    """Same repo, same dates, a shorter fuse — the figure `specky check` gates on is the figure
    the viewer badges on, so it's configured in one place."""
    _link(tmp_repo, doc_days_ago=30)
    _commit(tmp_repo, "small follow-up", {"src/app.py": "v9\n"}, days_ago=27)
    (tmp_repo / "pyproject.toml").write_text("[tool.specky.check]\nstale_after_days = 1\n")
    run_index(tmp_repo)
    assert _row(tmp_repo) != ("", "")


def test_staleness_costs_two_git_processes_whatever_the_history(stale_repo, monkeypatch):
    """The bound that makes this safe to run on someone else's monorepo: the doc side is limited
    to `specs/`, the code side to the span that can produce staleness at all."""
    for i in range(12):
        _commit(stale_repo, f"unrelated {i}", {f"src/other{i}.py": "x\n"}, days_ago=40 - i)

    calls: list[list[str]] = []
    real = gitlog.run

    def counting(root, args, **kwargs):
        calls.append(args)
        return real(root, args, **kwargs)

    monkeypatch.setattr(gitlog, "run", counting)

    conn = connect(stale_repo)
    try:
        index_staleness(stale_repo, conn, stale_after_days=14)
    finally:
        conn.close()
    assert len(calls) == 2, calls


def test_the_code_side_walk_is_windowed_to_the_oldest_covered_doc(stale_repo, monkeypatch):
    captured: list[str] = []
    real = staleness.last_code_changes
    monkeypatch.setattr(
        staleness,
        "last_code_changes",
        lambda root, files, since: captured.append(since) or real(root, files, since),
    )
    conn = connect(stale_repo)
    try:
        index_staleness(stale_repo, conn, stale_after_days=14)
    finally:
        conn.close()
    assert captured
    behind = (datetime.now(timezone.utc) - datetime.fromisoformat(captured[0])).days
    assert 59 <= behind <= 61


# --- what `specky check` does with the verdict: report it, never fail on it


def test_check_reports_a_stale_doc_as_advice_not_a_violation(stale_repo):
    """The range documents itself via a sibling doc in the same domain, so there's no violation —
    and the doc that had already fallen behind is still worth saying while someone is in there."""
    base = git(stale_repo, "rev-parse", "HEAD").strip()
    _commit(
        stale_repo,
        "change the code, add a sibling doc",
        {"src/app.py": "v10\n", "specs/billing/refund-limits.md": "# Limits\n"},
    )

    report = run_check(stale_repo, base=base)
    assert report.violations == ()
    assert [s.doc_path for s in report.stale] == [DOC]
    assert report.stale_elsewhere == 0


def test_a_stale_doc_the_range_updated_is_not_reported(stale_repo):
    base = git(stale_repo, "rev-parse", "HEAD").strip()
    _commit(stale_repo, "change the code and the doc", {"src/app.py": "v10\n", DOC: "# New\n"})

    report = run_check(stale_repo, base=base)
    assert report.stale == ()
    assert report.stale_elsewhere == 0


def test_stale_docs_outside_the_range_are_counted_not_listed(stale_repo):
    base = git(stale_repo, "rev-parse", "HEAD").strip()
    _commit(stale_repo, "touch something else entirely", {"src/unrelated.py": "y\n"})

    report = run_check(stale_repo, base=base)
    assert report.stale == ()
    assert report.stale_elsewhere == 1
    assert report.as_dict()["stale_elsewhere"] == 1


# --- and what the viewer does with it


def test_the_viewer_badges_the_stale_doc_and_offers_the_facet(stale_repo):
    site = render_site(stale_repo).parent
    page = next(p for p in site.glob("*.html") if "refund-flow" in p.name).read_text()
    assert "days behind code" in page
    assert 'data-facet="stale"' in page  # the rail's filter chip, on every page
    assert 'data-stale="true"' in page  # the nav entry the facet filters on


def test_the_viewer_offers_no_stale_facet_when_nothing_is_behind(tmp_repo, write_doc):
    write_doc("billing/refund-flow.md", DOC_BODY)
    run_index(tmp_repo)
    site = render_site(tmp_repo).parent
    text = "\n".join(p.read_text() for p in sorted(site.rglob("*.html")))
    assert 'data-facet="stale"' not in text
    assert 'data-stale="false"' in text
