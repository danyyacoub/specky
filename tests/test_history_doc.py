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
        json.dumps({"headline": "Two\nlines", "impact": "Severe", "what_changed": "x"})
    )
    assert doc.headline == "Two lines"
    assert doc.impact == ""


def test_breaking_is_an_impact():
    doc = commit_doc.parse_micro_doc(
        json.dumps({"headline": "Drops --legacy", "impact": "Breaking", "what_changed": "x"})
    )
    assert doc.impact == "breaking"


EXAMPLE = {
    "scenario": "A customer asks for a 120 EUR refund on a 100 EUR order",
    "before": "The refund went through.",
    "after": "It is refused with\n'refund exceeds order total'.",
}


def test_the_example_is_read_and_each_part_is_one_line():
    doc = commit_doc.parse_micro_doc(json.dumps({**json.loads(STRUCTURED), "example": EXAMPLE}))
    assert doc.scenario == EXAMPLE["scenario"]
    assert doc.before == "The refund went through."
    assert doc.after == "It is refused with 'refund exceeds order total'."
    assert "120 EUR" in doc.text()


@pytest.mark.parametrize(
    "example", ["a string", ["a", "list"], {}, {"scenario": "only a scenario"}, None]
)
def test_a_malformed_or_empty_example_is_no_example(example):
    doc = commit_doc.parse_micro_doc(json.dumps({**json.loads(STRUCTURED), "example": example}))
    assert (doc.scenario, doc.before, doc.after) == ("", "", "")
    assert doc.headline  # the rest of the reply still stands


def test_an_example_round_trips_between_what_changed_and_why(tmp_repo):
    doc = commit_doc.parse_micro_doc(json.dumps({**json.loads(STRUCTURED), "example": EXAMPLE}))
    text = commit_doc.write_history_file(tmp_repo, _commit_obj(), doc).read_text()
    assert text.index("## What changed") < text.index("## Example") < text.index("## Why")
    assert "**Scenario:** A customer asks" in text and "- **Before:** The refund went through." in text
    assert commit_doc.read_history(text) == ("c" * 40, replace(doc, commits=["c" * 40]))


def test_an_entry_being_extended_shows_the_model_its_example():
    doc = commit_doc.parse_micro_doc(json.dumps({**json.loads(STRUCTURED), "example": EXAMPLE}))
    _, prompt = commit_doc.extend_prompt(doc, ["first"], _commit_obj())
    assert "Example: A customer asks for a 120 EUR refund" in prompt
    assert "example" in commit_doc.MICRO_DOC_PREFIX and "breaking" in commit_doc.MICRO_DOC_PREFIX


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


# --- commits that are never pending ---------------------------------------------------------
#
# `never_documented` is the one rule `pending_commits`, `check` and `doctor` share, so that a
# history doc deleted on purpose stays gone instead of being regenerated by the next hook fire.


@pytest.mark.parametrize("tag", ["[skip ci]", "[ci skip]", "[skip specky]"])
def test_a_skip_tagged_commit_is_never_pending(in_repo, tag):
    sha = _commit(in_repo, f"chore: bump the version {tag}")

    assert sha not in [s for s, _ in commit_doc.pending_commits(in_repo)]
    assert sha not in [s for s, _ in _undocumented_commits(in_repo, "HEAD~1")]


def test_a_bot_authored_commit_is_never_pending(in_repo):
    """CI's own commits — `github-actions[bot]`, `dependabot[bot]` — carry no business logic to
    document, so asking for a history doc for one would only cost a model call."""
    (in_repo / "ci.txt").write_text("x")
    git(in_repo, "add", "-A")
    git(
        in_repo,
        "commit", "-q", "-m", "chore: bump deps",
        "--author=github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>",
    )
    sha = git(in_repo, "rev-parse", "HEAD").strip()

    assert sha not in [s for s, _ in commit_doc.pending_commits(in_repo)]


def test_a_subject_matching_history_ignore_is_never_pending(in_repo):
    """`[history] ignore` is how a repo retires a class of commits — "chore: bump*", "docs:*" —
    without the hook writing them back the next time it fires."""
    (in_repo / "specky.toml").write_text('[history]\nignore = ["chore: bump*"]\n')
    ignored = _commit(in_repo, "chore: bump the version")
    wanted = _commit(in_repo, "feat: cap refunds")

    shas = [s for s, _ in commit_doc.pending_commits(in_repo)]
    assert ignored not in shas
    assert wanted in shas  # the same rule must not eat real work


def test_deleting_an_ignored_commits_doc_sticks(in_repo):
    """The point of `ignore`: remove the doc and the commit doesn't come back as pending."""
    (in_repo / "specky.toml").write_text('[history]\nignore = ["chore: bump*"]\n')
    sha = _commit(in_repo, "chore: bump the version")
    doc_path = commit_doc.write_history_file(
        in_repo,
        commit_doc._commit_info(sha, with_diff=False),
        commit_doc.MicroDoc(headline="Bumped", what="w"),
    )
    doc_path.unlink()

    assert sha not in [s for s, _ in commit_doc.pending_commits(in_repo)]


# --- business logic only --------------------------------------------------------------------
#
# Only business logic gets a history doc. A commit touching no business file is dropped before any
# provider call; any other commit's micro-doc call can answer `{"skip": true}`, which stops it there
# and puts it in the skip ledger so it isn't paid for again.

SKIP = json.dumps({"skip": True})


def _commit_files(repo: Path, message: str, files: dict[str, str]) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def test_a_commit_touching_only_non_business_files_is_never_pending(in_repo, business_gate):
    chores = _commit_files(
        in_repo,
        "update tests, docs and the lockfile",
        {
            "tests/test_refunds.py": "x",
            "README.md": "y",
            "package-lock.json": "{}",
            "infra/lb.tf": "z",
        },
    )
    mixed = _commit_files(
        in_repo, "cap refunds", {"src/refunds.py": "LIMIT = 1", "tests/test_limit.py": "x"}
    )

    shas = [s for s, _ in commit_doc.pending_commits(in_repo)]
    assert chores not in shas
    assert mixed in shas
    assert chores not in [s for s, _ in _undocumented_commits(in_repo, "HEAD~2")]


def test_the_docs_tree_is_never_business_logic(in_repo):
    sha = _commit_files(in_repo, "document refunds", {"specs/billing/refunds.md": "# Refunds\n"})

    assert sha not in [s for s, _ in commit_doc.pending_commits(in_repo)]


def test_history_paths_is_an_allowlist(in_repo, business_gate):
    (in_repo / "specky.toml").write_text('[history]\npaths = ["api/*"]\n')
    script = _commit_files(in_repo, "add a data script", {"scripts/export.py": "x"})
    rule = _commit_files(in_repo, "cap refunds", {"api/refunds.py": "LIMIT = 1"})

    shas = [s for s, _ in commit_doc.pending_commits(in_repo)]
    assert script not in shas
    assert rule in shas


def test_exclude_paths_adds_to_the_defaults_and_a_bang_re_includes(tmp_repo, business_gate):
    (tmp_repo / "specky.toml").write_text(
        '[history]\nexclude_paths = ["scripts/*", "!skills/*.md"]\n'
    )
    config = commit_doc.HistoryConfig.load(tmp_repo)

    assert not config.is_business_file("scripts/export.py")
    assert config.is_business_file("skills/refunds/SKILL.md")
    assert not config.is_business_file("README.md")
    assert not config.is_business_file("api/tests/test_refunds.py")
    assert config.is_business_file("api/refunds.py")
    assert not config.touches_business([])


def test_a_skip_reply_writes_no_doc_and_asks_nothing_more(in_repo, monkeypatch):
    sha = _commit(in_repo, "rename a helper")
    classification = json.dumps(
        {"skip": False, "domain": "billing", "topic": "refunds", "type": "feature", "tags": []}
    )
    provider = RoutingProvider(summary=SKIP, classification=classification)
    _use_provider(monkeypatch, provider)

    commit_doc.sync(assume_yes=True, commit=True)

    history = paths.history_dir(in_repo)
    assert commit_doc.history_doc_for(history, sha) is None
    assert not list(history.glob("*.md"))
    assert all(p.startswith(commit_doc.MICRO_DOC_PREFIX) for p in provider.prompts), (
        "no classification and no feature doc for a commit with no business logic"
    )
    assert not (in_repo / "specs" / "billing").exists()
    assert commit_doc.pending_commits(in_repo) == []
    # The ledger is committed with the doc-sync commit, which credits no code to a skipped commit.
    assert f"{sha} rename a helper" in (history / commit_doc.SKIPPED_LEDGER).read_text()
    assert "specs/history/skipped.txt" in git(in_repo, "show", "--name-only", "--format=", "HEAD")
    assert commit_doc.DOCUMENTS_TRAILER not in git(in_repo, "log", "-1", "--format=%B")

    calls = len(provider.prompts)
    commit_doc.sync(assume_yes=True)
    assert len(provider.prompts) == calls, "a skipped commit is never paid for twice"


def test_deleting_a_ledger_line_makes_the_commit_pending_again(in_repo):
    sha = _commit(in_repo, "rename a helper")
    index = commit_doc.HistoryIndex(paths.history_dir(in_repo))
    ledger = index.skip(sha, "rename a helper")
    assert sha not in [s for s, _ in commit_doc.pending_commits(in_repo)]

    ledger.write_text("\n".join(l for l in ledger.read_text().splitlines() if sha not in l))

    assert sha in [s for s, _ in commit_doc.pending_commits(in_repo)]


def test_record_commit_takes_a_skip_from_the_session_agent(in_repo):
    sha = _commit(in_repo, "bump the linter")

    path = commit_doc.record_commit(in_repo, sha, SKIP)

    assert path.name == commit_doc.SKIPPED_LEDGER
    assert sha not in [s for s, _ in commit_doc.pending_commits(in_repo)]
    assert not list(paths.history_dir(in_repo).glob("*.md"))


def test_a_rebase_carries_a_skipped_commit_onto_its_new_sha(in_repo):
    sha = _commit(in_repo, "rename a helper")
    commit_doc.HistoryIndex(paths.history_dir(in_repo)).skip(sha, "rename a helper")
    git(in_repo, "commit", "-q", "--amend", "-m", "rename a helper, again")
    new_sha = git(in_repo, "rev-parse", "HEAD").strip()

    [(old, new)] = commit_doc.apply_rewrites(in_repo, f"{sha} {new_sha}\n")

    assert old == new == paths.history_dir(in_repo) / commit_doc.SKIPPED_LEDGER
    text = new.read_text()
    assert new_sha in text and sha not in text
    assert new_sha not in [s for s, _ in commit_doc.pending_commits(in_repo)]


def test_the_prompts_see_only_the_business_files_diff(in_repo, business_gate, monkeypatch):
    _commit_files(
        in_repo,
        "cap refunds",
        {
            "specs/billing/refunds.md": "SPEC_TEXT\n",
            "tests/test_refunds.py": "TEST_TEXT\n",
            "src/refunds.py": "RULE_TEXT = 1\n",
        },
    )
    provider = RoutingProvider(summary=STRUCTURED)
    _use_provider(monkeypatch, provider)

    commit_doc.sync(assume_yes=True)

    [micro] = [p for p in provider.prompts if p.startswith(commit_doc.MICRO_DOC_PREFIX)]
    assert "RULE_TEXT" in micro and "- src/refunds.py" in micro
    assert "TEST_TEXT" not in micro and "SPEC_TEXT" not in micro
    classify = [p for p in provider.prompts if not p.startswith(commit_doc.MICRO_DOC_PREFIX)]
    assert classify and all("TEST_TEXT" not in p for p in classify)


def test_a_skip_reply_is_parsed_as_a_skip():
    assert commit_doc.parse_micro_doc('```json\n{"skip": true}\n```').skip
    assert not commit_doc.parse_micro_doc(STRUCTURED).skip
    assert not commit_doc.parse_micro_doc('{"skip": "no"}').skip


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
