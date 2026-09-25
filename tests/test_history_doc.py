"""The history doc as a structured record: what the micro-doc call returns, what the file holds,
and who reads it back — the index, the Spec Assistant's history search, `commits_for_doc`.

The legacy shape (`# Commit <sha8>` and one paragraph) still has to read, because every repo that
adopted specky before this has a directory of them and nothing rewrites them unasked.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from specky import catalog, commit_doc, db, paths
from specky.check import _undocumented_commits
from specky.indexer import run_index

from conftest import RoutingProvider, git


def _commit(repo: Path, message: str, name: str | None = None) -> str:
    (repo / (name or f"{len(list(repo.glob('*.txt')))}.txt")).write_text(message)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def in_repo(tmp_repo: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_repo)
    return tmp_repo


def _use_provider(monkeypatch, provider) -> None:
    monkeypatch.setattr(commit_doc, "load_provider_from_toml", lambda _path, _command="": provider)


def _commit_obj(message: str = "Cap refunds at the order total") -> commit_doc.Commit:
    return commit_doc.Commit(
        sha="c" * 40, author="Dev <d@example.com>", date="2026-09-01", message=message, diff=""
    )


STRUCTURED = json.dumps(
    {
        "headline": "Refunds can no longer exceed the order total",
        "impact": "fix",
        "what_changed": "A refund larger than the order used to go through. It is now refused.",
        "why": "Support reported double refunds on split shipments.",
    }
)


# --- the reply -----------------------------------------------------------------------------


def test_a_json_reply_becomes_a_structured_doc():
    doc = commit_doc.parse_micro_doc(STRUCTURED)
    assert doc.headline == "Refunds can no longer exceed the order total"
    assert doc.impact == "fix"
    assert doc.what.startswith("A refund larger")
    assert doc.why.startswith("Support reported")


def test_a_fenced_reply_is_still_json():
    assert commit_doc.parse_micro_doc(f"```json\n{STRUCTURED}\n```").impact == "fix"


def test_a_reply_that_isnt_json_is_kept_as_legacy_prose_not_guessed_at():
    """No headline invented for it: the legacy shape is what `--refresh-history` picks up again."""
    doc = commit_doc.parse_micro_doc("Refunds are capped now.")
    assert doc == commit_doc.MicroDoc(what="Refunds are capped now.")


def test_an_unknown_impact_is_dropped_and_a_multiline_headline_is_one_line():
    doc = commit_doc.parse_micro_doc(
        json.dumps({"headline": "Two\nlines", "impact": "Breaking", "what_changed": "x"})
    )
    assert doc.headline == "Two lines"
    assert doc.impact == ""


# --- the file ------------------------------------------------------------------------------


def test_a_structured_doc_round_trips(tmp_repo):
    doc = commit_doc.parse_micro_doc(STRUCTURED)
    doc.features = ["specs/billing/refund-limits.md"]
    path = commit_doc.write_history_file(tmp_repo, _commit_obj(), doc)
    text = path.read_text()

    assert "# Refunds can no longer exceed the order total\n" in text
    assert "impact: fix" in text and "features: [specs/billing/refund-limits.md]" in text
    assert "## What changed" in text and "## Why" in text
    assert "- **Message:** Cap refunds at the order total" in text
    # A legacy doc covers the one commit its `sha:` names.
    assert commit_doc.read_history(text) == ("c" * 40, replace(doc, commits=["c" * 40]))


def test_a_structured_doc_without_a_why_has_no_why_section(tmp_repo):
    doc = commit_doc.MicroDoc(headline="Adds refunds", impact="feature", what="Refunds exist.")
    text = commit_doc.write_history_file(tmp_repo, _commit_obj(), doc).read_text()
    assert "## Why" not in text
    assert commit_doc.read_history(text)[1] == replace(doc, commits=["c" * 40])


def test_a_legacy_doc_reads_as_prose_with_no_headline():
    text = (
        "---\nsha: " + "c" * 40 + "\n---\n\n# Commit cccccccc\n\n- **Date:** 2026-01-01\n"
        "- **Author:** a\n- **Message:** m\n\nRefunds are capped. They used to be unlimited.\n"
    )
    sha, doc = commit_doc.read_history(text)
    assert sha == "c" * 40
    assert doc == commit_doc.MicroDoc(
        what="Refunds are capped. They used to be unlimited.", commits=["c" * 40]
    )


def test_a_doc_with_nothing_after_its_metadata_is_not_guessed_at():
    assert commit_doc.read_history("# Commit cccccccc\n\n- **Date:** x\n") is None


@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        (commit_doc.MicroDoc(headline="Adds refunds", what="Long prose."), "Adds refunds"),
        (commit_doc.MicroDoc(what="Refunds are capped. They were not."), "Refunds are capped."),
        # `e.g.` and a version number are not sentence ends: the next word isn't a capital.
        (commit_doc.MicroDoc(what="Adds flags, e.g. `--since`, to v1.2 sync."), "Adds flags, e.g. `--since`, to v1.2 sync."),
    ],
)
def test_brief_is_the_headline_or_a_legacy_docs_first_sentence(doc, expected):
    assert commit_doc.brief(doc) == expected


def test_brief_is_bounded():
    line = commit_doc.brief(commit_doc.MicroDoc(what="word " * 100), limit=40)
    assert len(line) <= 41 and line.endswith("…")


# --- the sync path -------------------------------------------------------------------------


def test_sync_writes_a_structured_doc_linked_to_its_feature(in_repo, monkeypatch):
    """`features:` is written into the committed doc, not only into the gitignored database."""
    _commit(in_repo, "Cap refunds")
    classification = json.dumps(
        {"skip": False, "domain": "billing", "topic": "refund-limits", "type": "feature", "tags": []}
    )
    _use_provider(monkeypatch, RoutingProvider(summary=STRUCTURED, classification=classification))

    commit_doc.sync(assume_yes=True)

    head = git(in_repo, "rev-parse", "HEAD").strip()
    doc_path = commit_doc.history_doc_for(paths.history_dir(in_repo), head)
    _sha, doc = commit_doc.read_history(doc_path.read_text())
    assert doc.headline == "Refunds can no longer exceed the order total"
    assert doc.features == ["specs/billing/refund-limits.md"]


def test_a_failed_classification_still_leaves_the_history_doc(in_repo, monkeypatch):
    sha = _commit(in_repo, "Cap refunds")

    def boom(*_args, **_kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("specky.generator.sync_feature_doc", boom)
    _use_provider(monkeypatch, RoutingProvider(summary=STRUCTURED))

    commit_doc.sync(assume_yes=True)

    assert commit_doc.history_doc_for(paths.history_dir(in_repo), sha) is not None


def test_a_merge_commit_is_never_pending(in_repo):
    """Its `git show` is an empty combined diff; the branch's own commits carry what it did."""
    git(in_repo, "checkout", "-q", "-b", "feature")
    branch_sha = _commit(in_repo, "on the branch")
    git(in_repo, "checkout", "-q", "-")
    _commit(in_repo, "on main", name="main.txt")
    git(in_repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'feature'", "feature")
    merge = git(in_repo, "rev-parse", "HEAD").strip()

    shas = [sha for sha, _ in commit_doc.pending_commits(in_repo)]
    assert branch_sha in shas
    assert merge not in shas
    assert merge not in [sha for sha, _ in _undocumented_commits(in_repo, "HEAD~2")]


def test_refresh_rewrites_only_legacy_docs_and_keeps_their_link(in_repo, monkeypatch):
    legacy_sha = _commit(in_repo, "legacy one")
    modern_sha = _commit(in_repo, "modern one")
    commit_doc.write_history_file(
        in_repo,
        commit_doc._commit_info(legacy_sha, with_diff=False),
        commit_doc.MicroDoc(what="Old prose.", features=["specs/billing/refund-limits.md"]),
    )
    commit_doc.write_history_file(
        in_repo,
        commit_doc._commit_info(modern_sha, with_diff=False),
        commit_doc.MicroDoc(headline="Already structured", what="x"),
    )
    # Everything older is documented too, so only the two above are in play.
    for sha in git(in_repo, "rev-list", "HEAD~2").split():
        commit_doc.write_history_file(
            in_repo, commit_doc._commit_info(sha, with_diff=False), commit_doc.MicroDoc(headline="h")
        )
    provider = RoutingProvider(summary=STRUCTURED)
    _use_provider(monkeypatch, provider)

    commit_doc.sync(assume_yes=True, refresh_history=True)

    history = paths.history_dir(in_repo)
    _sha, refreshed = commit_doc.read_history(commit_doc.history_doc_for(history, legacy_sha).read_text())
    assert refreshed.headline == "Refunds can no longer exceed the order total"
    assert refreshed.features == ["specs/billing/refund-limits.md"]
    _sha, untouched = commit_doc.read_history(commit_doc.history_doc_for(history, modern_sha).read_text())
    assert untouched.headline == "Already structured"
    assert len(provider.prompts) == 1, "one micro-doc call, and no classification"


def test_a_rebase_keeps_impact_features_and_headline(in_repo):
    sha = _commit(in_repo, "original")
    doc = commit_doc.MicroDoc(
        headline="Adds refunds", impact="feature", what="w", why="y", features=["specs/a/b.md"]
    )
    commit_doc.write_history_file(in_repo, commit_doc._commit_info(sha, with_diff=False), doc)
    git(in_repo, "commit", "-q", "--amend", "-m", "amended")
    new_sha = git(in_repo, "rev-parse", "HEAD").strip()

    [(_old, new)] = commit_doc.apply_rewrites(in_repo, f"{sha} {new_sha}\n")

    assert commit_doc.read_history(new.read_text()) == (new_sha, replace(doc, commits=[new_sha]))


# --- the index -----------------------------------------------------------------------------


def test_the_index_reads_summaries_and_links_from_the_files_on_a_fresh_clone(in_repo, write_doc):
    """No `micro_docs` or `commit_links` rows — the state of any clone but the one that ran the
    hook — and the history search and `commits_for_doc` still answer."""
    write_doc("billing/refund-limits.md", "# Refund Limits\n\nCaps.\n", {"type": "feature", "tags": ["refunds"]})
    sha = _commit(in_repo, "Cap refunds")
    doc = commit_doc.parse_micro_doc(STRUCTURED)
    doc.features = ["specs/billing/refund-limits.md"]
    commit_doc.write_history_file(in_repo, commit_doc._commit_info(sha, with_diff=False), doc)

    run_index(in_repo)

    conn = db.connect(in_repo)
    try:
        summary, tags = conn.execute(
            "SELECT summary, tags FROM commits_fts WHERE sha = ?", (sha,)
        ).fetchone()
        headline, impact = conn.execute(
            "SELECT headline, impact FROM commits WHERE sha = ?", (sha,)
        ).fetchone()
    finally:
        conn.close()
    assert "split shipments" in summary
    assert tags == "refunds"
    assert (headline, impact) == ("Refunds can no longer exceed the order total", "fix")

    [row] = catalog.commits_for_doc(in_repo, "specs/billing/refund-limits.md")
    assert row["headline"] == headline
    assert row["impact"] == "fix"
    assert row["history_path"] == f"specs/history/{sha[:8]}.md"


def test_the_history_page_is_titled_by_its_headline(in_repo):
    sha = _commit(in_repo, "Cap refunds")
    commit_doc.write_history_file(
        in_repo, commit_doc._commit_info(sha, with_diff=False), commit_doc.parse_micro_doc(STRUCTURED)
    )
    run_index(in_repo)

    conn = db.connect(in_repo)
    try:
        [title] = conn.execute(
            "SELECT title FROM documents WHERE path = ?", (f"specs/history/{sha[:8]}.md",)
        ).fetchone()
    finally:
        conn.close()
    assert title == "Refunds can no longer exceed the order total"
