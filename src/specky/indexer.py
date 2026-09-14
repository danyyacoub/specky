"""Populates the SQLite index (documents + commits + their FTS5 tables) from what's
actually on disk and in git log. `specky index` does a full rebuild each run rather than
an incremental sync — a repo's specs/ tree and git log are cheap enough to re-walk, and
a full rebuild can't drift from reality the way incremental updates could.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from specky import frontmatter, gitlog, paths
from specky.check import CheckConfig
from specky.commit_doc import _AUTO_COMMIT_MARKER
from specky.db import connect, fts_match_query
from specky.staleness import index_staleness


def _domain_for(md_path: Path, specs_root: Path) -> str:
    rel = md_path.relative_to(specs_root)
    return rel.parts[0] if len(rel.parts) > 1 else "root"


def _title_for(content: str, fallback: str) -> str:
    for line in content.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return fallback


def _reset_tables(conn) -> None:
    # micro_docs and commit_links (written incrementally by commit_doc.py) and prompt_cache/usage
    # (written by the caching provider) aren't derived from a rescan, so they're deliberately left
    # out of this wipe — re-indexing must not throw away a paid-for AI response. doc_files *is*
    # derived (from git log), so it gets rebuilt with everything else; the staleness columns live
    # on `documents` and are rewritten with the rows themselves.
    conn.execute("DELETE FROM documents")
    conn.execute("DELETE FROM documents_fts")
    conn.execute("DELETE FROM commits")
    conn.execute("DELETE FROM commits_fts")
    conn.execute("DELETE FROM doc_files")


def index_documents(repo_root: Path, conn) -> int:
    specs_root = paths.docs_root(repo_root)
    if not specs_root.exists():
        return 0

    count = 0
    for md_path in sorted(specs_root.rglob("*.md")):
        raw = md_path.read_text()
        meta, content = frontmatter.parse(raw)
        domain = _domain_for(md_path, specs_root)
        title = _title_for(content, md_path.stem)
        rel_path = str(md_path.relative_to(repo_root))
        updated_at = datetime.fromtimestamp(md_path.stat().st_mtime, tz=timezone.utc).isoformat()
        doc_type = meta.get("type", "") if isinstance(meta.get("type", ""), str) else ""
        tags = ",".join(meta.get("tags", []))
        related = ",".join(meta.get("related", []))
        # A list-valued `owner: [a, b]` joins back to a string: everything downstream displays the
        # owner rather than matching on it, so one field of free text is all it has to be.
        owner = meta.get("owner", "")
        owner = ", ".join(owner) if isinstance(owner, list) else str(owner)

        conn.execute(
            "INSERT INTO documents "
            "(path, domain, title, content, doc_type, tags, related, owner, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rel_path, domain, title, content, doc_type, tags, related, owner, updated_at),
        )
        conn.execute(
            "INSERT INTO documents_fts (path, domain, title, content, tags) VALUES (?, ?, ?, ?, ?)",
            (rel_path, domain, title, content, tags),
        )
        count += 1
    return count


def index_commits(repo_root: Path, conn) -> int:
    log = gitlog.run(repo_root, ["log", "--format=%H%x1f%an <%ae>%x1f%aI%x1f%s", "--reverse"])

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
        tag_rows = conn.execute(
            "SELECT DISTINCT documents.tags FROM commit_links "
            "JOIN documents ON documents.path = commit_links.path "
            "WHERE commit_links.sha = ? AND documents.tags != ''",
            (sha,),
        ).fetchall()
        tags = ",".join(sorted({t for (row_tags,) in tag_rows for t in row_tags.split(",") if t}))
        conn.execute(
            "INSERT INTO commits_fts (sha, message, summary, tags) VALUES (?, ?, ?, ?)",
            (sha, subject, summary, tags),
        )
        count += 1
    return count


# A commit that touched more files than this contributes none of them to doc_files. A bulk
# rename, a vendored-dependency drop or an initial import can name thousands of files, and a doc
# linked to such a commit is not evidence that the doc describes each of them — it would just
# make `specky check` demand that one doc be updated for edits all over the repo.
DOC_FILES_MAX_PER_COMMIT = 50

# The same bound from the other side: a commit that rewrote more covering docs than this is a
# batch regeneration or a bulk doc import, not one change being documented. Pairing every file it
# touched with every doc it rewrote is what makes `specky check` demand an unrelated doc — on
# specky's own repo, three such commits produced 12 of 13 reported violations.
DOC_FILES_MAX_DOCS_PER_COMMIT = 3

def _not_coverable(repo_root: Path) -> tuple[str, ...]:
    """specky's own outputs, which no doc "describes": the docs themselves, and the index/site
    under `.specky/` in a repo that forgot to gitignore it (check.py ignores these too, but a pair
    recorded here would still be reported as coverage the repo doesn't have)."""
    return (paths.docs_prefix(repo_root), ".specky/")


def doc_commits(repo_root: Path) -> list[tuple[str, list[str], str, list[str]]]:
    """`(sha, parents, subject, doc paths)` for every commit that touched the docs tree.

    Path-limited, so both the walk and the file lists stay proportional to the number of
    doc-producing commits rather than to the length of history.
    """
    log = gitlog.run(
        repo_root,
        [
            "log",
            "--format=%x01%H%x1f%P%x1f%s",
            "--name-only",
            "--",
            paths.docs_prefix(repo_root),
        ],
    )
    commits = []
    for lines in gitlog.blocks(log):
        sha, parents, subject = lines[0].split("\x1f", 2)
        commits.append((sha, parents.split(), subject, [f for f in lines[1:] if f]))
    return commits


def _documented_revs(
    sha: str, parents: list[str], subject: str, docs: list[str], history_prefix: str
) -> list[str]:
    """Which commits' code the docs in this commit describe.

    specky's own flow splits the two: the code lands in one commit and the hook's follow-up
    commits the docs, so a doc-sync commit's docs describe *other* commits. The history docs in
    it say which — `specs/history/<sha>.md` is named for the commit it documents, and one
    `specky sync` backfill can carry hundreds of them. Failing that, an auto-commit's docs
    belong to the commit it followed. Any other commit (a hand-written doc, an agent that
    committed code and docs together) describes itself.
    """
    if stems := [Path(d).stem for d in docs if d.startswith(history_prefix)]:
        return stems
    return parents[:1] if subject.startswith(_AUTO_COMMIT_MARKER) else [sha]


def _resolve_revs(repo_root: Path, revs: list[str]) -> dict[str, str]:
    """`{rev as written: full sha}`, dropping anything that isn't a commit in this repo.

    History docs are named for an *abbreviated* sha, and one can be missing (a rebased or
    dropped commit) or ambiguous. `cat-file --batch-check` resolves the whole batch in one
    process and answers per input line, so neither case needs a git call of its own.
    """
    if not revs:
        return {}
    out = gitlog.run(repo_root, ["cat-file", "--batch-check"], stdin="\n".join(revs))
    resolved = {}
    for rev, line in zip(revs, out.splitlines()):
        oid, _, kind = line.partition(" ")
        if kind.startswith("commit"):
            resolved[rev] = oid
    return resolved


def _code_files(repo_root: Path, shas: list[str]) -> dict[str, list[str]]:
    """`{sha: code files it touched}` for the given commits, in one git process."""
    if not shas:
        return {}
    log = gitlog.run(
        repo_root,
        ["log", "--no-walk", "--stdin", "--format=%x01%H", "--name-only"],
        stdin="\n".join(shas),
    )
    return {
        lines[0]: [f for f in lines[1:] if f and not f.startswith(_not_coverable(repo_root))]
        for lines in gitlog.blocks(log)
    }


def index_doc_files(repo_root: Path, conn) -> int:
    """Map code files to the feature/workflow docs that cover them, from git alone.

    Derived from committed history on purpose: `.specky/` is gitignored, so anything read out of
    this database instead — `commit_links`, which the post-commit hook writes — is empty on a
    fresh clone, and `specky check` in CI would then have nothing to enforce. Everything here
    comes from `git log`, so a CI run and a developer's checkout compute the same map.

    Three git processes, none of them proportional to the length of history: the `specs/`-limited
    walk, one `cat-file --batch-check` to resolve the history docs' abbreviated shas, and one
    `git log --stdin` for the file lists of the commits they name.

    Each pair carries how many separate commits produced it, because one commit pairing a file
    with a doc is weak evidence — every file in that commit gets paired with it, including the
    incidental ones. check.py is what decides how much recurrence a gate failure needs.
    """
    commits = doc_commits(repo_root)
    if not commits:
        return 0

    history_prefix = paths.history_prefix(repo_root)
    # A history doc is a per-commit narrative and the root docs (MODULES, GLOSSARY, PRODUCT) are
    # indexes; neither "covers" a code file. What's left is <docs root>/<domain>/<topic>.md.
    covering = {
        sha: docs
        for sha, _, _, all_docs in commits
        if 0 < len(docs := [d for d in all_docs if d.count("/") >= 2 and not d.startswith(history_prefix)])
        <= DOC_FILES_MAX_DOCS_PER_COMMIT
    }
    targets = {
        sha: _documented_revs(sha, parents, subject, docs, history_prefix)
        for sha, parents, subject, docs in commits
        if sha in covering
    }
    resolved = _resolve_revs(repo_root, sorted({r for revs in targets.values() for r in revs}))
    files_by_sha = _code_files(repo_root, sorted(set(resolved.values())))

    pairs: Counter[tuple[str, str]] = Counter()
    for sha, revs in targets.items():
        for rev in revs:
            files = files_by_sha.get(resolved.get(rev, ""), ())
            if not files or len(files) > DOC_FILES_MAX_PER_COMMIT:
                continue
            pairs.update((f, doc_path) for doc_path in covering[sha] for f in files)

    conn.executemany(
        "INSERT OR REPLACE INTO doc_files (path, doc_path, commits) VALUES (?, ?, ?)",
        [(path, doc_path, commits) for (path, doc_path), commits in pairs.items()],
    )
    return len(pairs)


def run_index(repo_root: Path) -> tuple[int, int]:
    conn = connect(repo_root)
    try:
        _reset_tables(conn)
        doc_count = index_documents(repo_root, conn)
        commit_count = index_commits(repo_root, conn)
        index_doc_files(repo_root, conn)
        # Last: staleness reads the doc_files rows the step above just wrote. The threshold lives
        # in `[check]` because `specky check` reports the same verdict this stores.
        index_staleness(repo_root, conn, CheckConfig.load(repo_root).stale_after_days)
        conn.commit()
        return doc_count, commit_count
    finally:
        conn.close()


def search(repo_root: Path, query: str, limit: int = 10) -> list[dict]:
    match = fts_match_query(query)
    if match is None:
        return []

    conn = connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT path, domain, title, snippet(documents_fts, 3, '>>', '<<', '…', 24) "
            "FROM documents_fts WHERE documents_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
        return [
            {"path": path, "domain": domain, "title": title, "snippet": snippet}
            for path, domain, title, snippet in rows
        ]
    finally:
        conn.close()
