"""Is this doc still true? — computed from git dates alone, no AI call.

The first thing a reader of generated docs asks is whether they can trust the page, and the
cheapest honest answer is arithmetic: if the code a doc covers changed well after the doc last
did, say so on the page instead of letting the reader find out the hard way.

A doc's coverage comes from the `doc_files` map indexer.py already derives, so this pass adds no
knowledge of its own — only two dates per doc:

- `last_doc_change`: the newest commit that touched the doc.
- `last_code_change`: the newest commit that touched any file the doc covers.

Stale when the second is more than `[check] stale_after_days` past the first. Docs no `doc_files`
row mentions are neither stale nor fresh — nothing is known about them, and inventing a verdict
would be worse than leaving the badge off.

**Cost.** Two git processes, whatever the repo's size. The doc side is path-limited to `specs/`.
The code side is limited to commits *since the oldest last_doc_change among covered docs*, which
is the only span that can make anything stale: a file whose last change predates every covered
doc's own last change is, by definition, not behind. So an actively documented repo walks days of
history here, not years — and a repo whose docs are all ancient pays a longer walk once, on the
run that tells it everything is stale.

Both sides use the *committer* date (`%cI`), not the author date: a rebased or cherry-picked
commit keeps its original author date, which would make code that landed after a doc look older
than it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from specky import gitlog

# `%cI` is strict ISO 8601 with an offset, which datetime.fromisoformat reads directly. Dates are
# compared as datetimes, never as strings: two commits an hour apart in different timezones sort
# the wrong way round as text.
_FORMAT = "--format=%x01%cI"


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _newest_per_path(log: str, keep: set[str] | None = None) -> dict[str, str]:
    """`{path: ISO date it last changed}` from one newest-first `--name-only` log.

    `keep` bounds memory to the paths that matter: on a large repo the covered set is a small
    fraction of the tree, and dates for the rest would be collected and thrown away.
    """
    dates: dict[str, str] = {}
    for lines in gitlog.blocks(log):
        date = lines[0]
        for path in lines[1:]:
            if path and (keep is None or path in keep):
                dates.setdefault(path, date)  # newest-first, so the first sighting wins
    return dates


def last_doc_changes(repo_root: Path) -> dict[str, str]:
    log = gitlog.run(repo_root, ["log", _FORMAT, "--name-only", "--", "specs/"])
    return _newest_per_path(log)


def last_code_changes(repo_root: Path, files: set[str], since: str) -> dict[str, str]:
    if not files:
        return {}
    log = gitlog.run(repo_root, ["log", _FORMAT, "--name-only", f"--since={since}"])
    return _newest_per_path(log, keep=files)


def _covered_files(conn) -> dict[str, set[str]]:
    """`{doc path: code files it covers}` — the `doc_files` map, read the other way round."""
    covered: dict[str, set[str]] = {}
    for doc_path, path in conn.execute("SELECT doc_path, path FROM doc_files"):
        covered.setdefault(doc_path, set()).add(path)
    return covered


def stale_docs(
    covered: dict[str, set[str]],
    doc_dates: dict[str, str],
    code_dates: dict[str, str],
    stale_after: timedelta,
) -> dict[str, tuple[str, str]]:
    """`{doc path: (last_doc_change, last_code_change)}` for docs that fell behind their code."""
    stale = {}
    for doc_path, files in covered.items():
        doc_date = doc_dates.get(doc_path)
        newest = max((code_dates[f] for f in files if f in code_dates), default=None, key=_parse)
        if doc_date and newest and _parse(newest) - _parse(doc_date) > stale_after:
            stale[doc_path] = (doc_date, newest)
    return stale


def index_staleness(repo_root: Path, conn, stale_after_days: int) -> int:
    """Fill `documents.stale_since` / `.last_code_change`, and return how many docs are stale.

    The verdict is stored rather than recomputed per reader because both readers — the HTML viewer
    and `specky check` — want the same answer, and neither should shell out to git to get it. The
    threshold arrives from the caller (`[check] stale_after_days`, read by run_index) so that this
    module stays git-and-SQL only, with no opinion about configuration.
    """
    covered = _covered_files(conn)
    if not covered:
        return 0

    doc_dates = last_doc_changes(repo_root)
    dated = [doc_dates[d] for d in covered if d in doc_dates]
    if not dated:
        return 0

    files = {f for fs in covered.values() for f in fs}
    code_dates = last_code_changes(repo_root, files, since=min(dated, key=_parse))
    stale = stale_docs(covered, doc_dates, code_dates, timedelta(days=stale_after_days))

    conn.executemany(
        "UPDATE documents SET stale_since = ?, last_code_change = ? WHERE path = ?",
        [(doc_date, code_date, doc) for doc, (doc_date, code_date) in stale.items()],
    )
    return len(stale)


def days_behind(stale_since: str, last_code_change: str) -> int:
    """How far a stale doc's code ran ahead of it, rounded down to whole days."""
    return (_parse(last_code_change) - _parse(stale_since)).days
