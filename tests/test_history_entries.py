"""History entries: one doc for a branch's commits, named for the branch or the subject.

With the hook on every commit, one doc per commit is one doc per "wip" and "fix typo". A branch's
own recent commits share an entry instead, each one rewriting it to describe the change as a
whole, and so do one person's commits straight onto a long-lived branch — until the entry's first
commit is `[history] window_days` old, so a week of hotfixes on main isn't one ever-growing file.

Every test here builds a real history. Commits carry explicit authors (`ALICE`, `BOB`) so that the
fixture's initial commit, by `Test`, never shares an entry with them on the default branch.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from specky import catalog, commit_doc, db, paths
from specky.indexer import _documented_revs, run_index

from conftest import RoutingProvider

ALICE = ("Alice", "alice@example.com")
BOB = ("Bob", "bob@example.com")

SUMMARY = json.dumps(
    {
        "headline": "Refunds are capped per plan",
        "impact": "feature",
        "what_changed": "A refund over the plan's limit is refused.",
        "why": "",
    }
)


def git(repo: Path, *args: str, who=ALICE, date: datetime | None = None) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": who[0],
        "GIT_AUTHOR_EMAIL": who[1],
        "GIT_COMMITTER_NAME": who[0],
        "GIT_COMMITTER_EMAIL": who[1],
    }
    if date is not None:
        env |= {"GIT_AUTHOR_DATE": date.isoformat(), "GIT_COMMITTER_DATE": date.isoformat()}
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    ).stdout


def commit(
    repo: Path, message: str, who=ALICE, date: datetime | None = None, name: str | None = None
) -> str:
    (repo / (name or f"{len(list(repo.glob('*.txt')))}.txt")).write_text(message)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, who=who, date=date)
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def in_repo(tmp_repo: Path, monkeypatch, consolidating) -> Path:
    monkeypatch.chdir(tmp_repo)
    return tmp_repo


@pytest.fixture
def provider(monkeypatch) -> RoutingProvider:
    routed = RoutingProvider(summary=SUMMARY)
    monkeypatch.setattr(commit_doc, "load_provider_from_toml", lambda _path, _command="": routed)
    return routed


def history(repo: Path) -> dict[str, commit_doc.MicroDoc]:
    """`{file name: what it says}` for every history doc."""
    return {
        p.name: commit_doc.read_history(p.read_text())[1]
        for p in sorted(paths.history_dir(repo).glob("*.md"))
    }


def default_branch(repo: Path) -> str:
    return git(repo, "symbolic-ref", "--short", "HEAD").strip()


def at(when: datetime, monkeypatch) -> None:
    monkeypatch.setattr(commit_doc, "_now", lambda: when)


def extend_calls(provider: RoutingProvider) -> list[str]:
    return [p for p in provider.prefixes if p == commit_doc.MICRO_DOC_EXTEND_PREFIX]


# --- the file ------------------------------------------------------------------------------


def _meta(sha: str, message: str, who=ALICE) -> commit_doc.Commit:
    return commit_doc.Commit(sha, f"{who[0]} <{who[1]}>", "2026-09-01T10:00:00+02:00", message, "")


def test_an_entry_of_several_commits_round_trips(tmp_repo):
    members = [_meta("a" * 40, "feat: refund limits"), _meta("b" * 40, "wip", BOB)]
    doc = commit_doc.parse_micro_doc(SUMMARY)
    doc.commits, doc.branch = [c.sha for c in members], "feat/refunds"
    doc.features = ["specs/billing/refund-limits.md"]

    path = commit_doc.write_entry(tmp_repo, doc, members, name="feat/refunds")

    assert path.name == "feat-refunds.md"
    text = path.read_text()
    assert f"commits: [{'a' * 40}, {'b' * 40}]" in text
    assert "branch: feat/refunds" in text
    assert "- **Author:** Alice <alice@example.com>, Bob <bob@example.com>" in text
    assert f"    - `{'b' * 8}` wip" in text  # four spaces: the viewer's Markdown nests it
    assert commit_doc.read_history(text) == ("a" * 40, doc)


def test_an_entry_of_one_commit_keeps_the_familiar_block(tmp_repo):
    doc = commit_doc.MicroDoc(headline="Adds refunds", what="x", commits=["c" * 40])
    text = commit_doc.write_entry(tmp_repo, doc, [_meta("c" * 40, "fix(api): Add refunds")]).read_text()

    assert "- **Message:** fix(api): Add refunds" in text
    assert commit_doc.read_history(text)[1] == doc


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("feat/Refund limits (v2)", "feat-refund-limits-v2"),
        ("Géré par l'équipe", "gere-par-l-equipe"),
        ("x" * 70, "x" * 60),
        ("word " * 20, ("word-" * 12).rstrip("-")),
        ("!!!", ""),
    ],
)
def test_entry_slug(name, expected):
    assert commit_doc.entry_slug(name) == expected


def test_a_new_entry_is_named_for_its_subject_without_the_commit_type(tmp_repo):
    history_dir = paths.history_dir(tmp_repo)
    history_dir.mkdir(parents=True)
    name = "fix(api)!: Unmatched lines"
    assert commit_doc.new_entry_path(history_dir, "a" * 40, name).name == "unmatched-lines.md"
    (history_dir / "unmatched-lines.md").write_text("taken")
    assert commit_doc.new_entry_path(history_dir, "a" * 40, name).name == "unmatched-lines-2.md"


def test_a_name_that_could_be_a_sha_or_is_empty_never_looks_like_a_legacy_doc(tmp_repo):
    history_dir = paths.history_dir(tmp_repo)
    assert commit_doc.new_entry_path(history_dir, "a" * 40, "deadbeef").name == "deadbeef-change.md"
    assert commit_doc.new_entry_path(history_dir, "a" * 40, "!!!").name == "change-aaaaaaaa.md"


def test_a_subject_named_entry_drops_the_conventional_commit_type(in_repo, provider, monkeypatch):
    monkeypatch.setattr(commit_doc, "HISTORY_CONSOLIDATE", "off")
    commit(in_repo, "fix(api): Unmatched lines always report full exposure")

    commit_doc.sync(assume_yes=True)

    assert "unmatched-lines-always-report-full-exposure.md" in history(in_repo)


def test_an_extension_that_isnt_json_keeps_what_the_entry_says(in_repo, monkeypatch):
    git(in_repo, "checkout", "-q", "-b", "feat/refunds")
    first = commit(in_repo, "feat: refund limits")
    replies = RoutingProvider(summary=SUMMARY)
    monkeypatch.setattr(commit_doc, "load_provider_from_toml", lambda _p, _c="": replies)
    commit_doc.sync(assume_yes=True)

    second = commit(in_repo, "wip")
    replies._summary = "Sorry, I can only answer in prose."
    commit_doc.sync(assume_yes=True)

    entry = history(in_repo)["feat-refunds.md"]
    assert entry.headline == "Refunds are capped per plan"
    assert entry.commits == [first, second]


# --- a feature branch ----------------------------------------------------------------------


def test_a_branchs_commits_share_one_entry_named_for_it(in_repo, provider):
    git(in_repo, "checkout", "-q", "-b", "feat/refund-limits")
    shas = [commit(in_repo, m) for m in ("feat: refund limits per plan", "wip", "fix typo")]

    commit_doc.sync(assume_yes=True)

    entry = history(in_repo)["feat-refund-limits.md"]
    assert entry.commits == shas
    assert entry.branch == "feat/refund-limits"
    assert not set(shas) & {sha for sha, _ in commit_doc.pending_commits(in_repo)}
    # The first commit opens it; each later one is asked to rewrite it, and is shown what it says.
    extensions = [
        prompt
        for prompt, prefix in zip(provider.prompts, provider.prefixes)
        if prefix == commit_doc.MICRO_DOC_EXTEND_PREFIX
    ]
    assert len(extensions) == 2
    assert all("Headline: Refunds are capped per plan" in p for p in extensions)
    assert "- feat: refund limits per plan\n- wip" in extensions[1]


def test_each_commit_extends_the_entry_as_the_hook_fires(in_repo, provider):
    git(in_repo, "checkout", "-q", "-b", "feat/refunds")
    first = commit(in_repo, "feat: refund limits")
    commit_doc.main()
    second = commit(in_repo, "address review")
    commit_doc.main()

    entry = history(in_repo)["feat-refunds.md"]
    assert entry.commits == [first, second]
    # Each doc-sync commit names the commit it documented, not the entry's first one.
    trailer = git(in_repo, "log", "-1", f"--format=%(trailers:key={commit_doc.DOCUMENTS_TRAILER},valueonly)")
    assert trailer.split() == [second]


def test_a_branch_older_than_the_window_starts_a_second_entry(in_repo, provider, monkeypatch):
    monday = datetime(2026, 9, 21, 10, tzinfo=timezone.utc)
    git(in_repo, "checkout", "-q", "-b", "feat/refunds")
    first = commit(in_repo, "feat: refund limits", date=monday)
    at(monday + timedelta(hours=1), monkeypatch)
    commit_doc.sync(assume_yes=True)

    later = commit(in_repo, "more review fixes", date=monday + timedelta(days=5))
    at(monday + timedelta(days=5, hours=1), monkeypatch)
    commit_doc.sync(assume_yes=True)

    docs = history(in_repo)
    assert docs["feat-refunds.md"].commits == [first]
    assert docs["feat-refunds-2.md"].commits == [later]


def test_a_stacked_branch_starts_its_own_entry(in_repo, provider):
    git(in_repo, "checkout", "-q", "-b", "feat/x")
    parent = commit(in_repo, "feat: x")
    commit_doc.sync(assume_yes=True)
    git(in_repo, "checkout", "-q", "-b", "feat/y")
    child = commit(in_repo, "feat: y on top of x")

    commit_doc.sync(assume_yes=True)

    docs = history(in_repo)
    assert docs["feat-x.md"].commits == [parent]
    assert docs["feat-y.md"].commits == [child]


def test_a_merged_branch_name_used_again_starts_afresh(in_repo, provider):
    main = default_branch(in_repo)
    git(in_repo, "checkout", "-q", "-b", "feat/x")
    first = commit(in_repo, "feat: x")
    commit_doc.sync(assume_yes=True)
    git(in_repo, "add", "-A")
    git(in_repo, "commit", "-q", "-m", commit_doc._AUTO_COMMIT_MARKER)
    git(in_repo, "checkout", "-q", main)
    git(in_repo, "merge", "-q", "--no-ff", "-m", "Merge feat/x", "feat/x")
    git(in_repo, "checkout", "-q", "feat/x")
    git(in_repo, "merge", "-q", "--ff-only", main)
    again = commit(in_repo, "feat: x, the sequel")

    commit_doc.sync(assume_yes=True)

    docs = history(in_repo)
    assert docs["feat-x.md"].commits == [first]
    assert docs["feat-x-2.md"].commits == [again]


def test_with_consolidation_off_every_commit_gets_its_own_entry(in_repo, provider, monkeypatch):
    (in_repo / "specky.toml").write_text('[history]\nconsolidate = "off"\n')
    git(in_repo, "checkout", "-q", "-b", "feat/x")
    commit(in_repo, "feat: one")
    commit(in_repo, "feat: two")

    commit_doc.sync(assume_yes=True)

    docs = history(in_repo)
    assert {"one.md", "two.md"} <= set(docs)
    assert not extend_calls(provider)


def test_a_backfill_older_than_the_window_is_one_entry_per_commit_and_stays_concurrent(
    in_repo, provider, monkeypatch
):
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    git(in_repo, "checkout", "-q", "-b", "feat/old")
    shas = [commit(in_repo, f"feat: step {i}", date=long_ago + timedelta(hours=i)) for i in range(3)]
    fetched: list[list[str]] = []
    real = commit_doc._prefetch_summaries
    monkeypatch.setattr(
        commit_doc,
        "_prefetch_summaries",
        lambda commits, p: fetched.append([c.sha for c in commits]) or real(commits, p),
    )

    commit_doc.sync(since=f"{shas[0]}^", assume_yes=True)

    assert {"step-0.md", "step-1.md", "step-2.md"} <= set(history(in_repo))
    assert set(shas) <= {sha for chunk in fetched for sha in chunk}
    assert not extend_calls(provider)


# --- a long-lived branch -------------------------------------------------------------------


def test_one_persons_hotfixes_share_an_entry_and_anothers_dont(in_repo, provider):
    fixes = [
        commit(in_repo, "fix(api): unmatched lines report full exposure", ALICE),
        commit(in_repo, "fix(api): badge stays conform", ALICE),
    ]
    theirs = commit(in_repo, "fix: rounding on credit notes", BOB)

    commit_doc.sync(assume_yes=True)

    docs = history(in_repo)
    entry = docs["unmatched-lines-report-full-exposure.md"]
    assert entry.commits == fixes
    assert entry.branch == default_branch(in_repo)
    assert docs["rounding-on-credit-notes.md"].commits == [theirs]


def test_a_commit_a_merge_brought_in_is_not_folded_into_a_hotfix(in_repo, provider):
    main = default_branch(in_repo)
    hotfix = commit(in_repo, "fix: hotfix", ALICE)
    git(in_repo, "checkout", "-q", "-b", "side")
    brought = commit(in_repo, "feat: side work", ALICE)
    git(in_repo, "checkout", "-q", main)
    git(in_repo, "merge", "-q", "--no-ff", "-m", "Merge side", "side")

    commit_doc.sync(assume_yes=True)

    docs = history(in_repo)
    assert docs["hotfix.md"].commits == [hotfix]
    assert docs["side-work.md"].commits == [brought]


def test_an_integration_branch_named_in_the_config_gets_the_long_lived_rules(in_repo, provider):
    """A `sprint` that pull requests merge into is shared like main: one author per entry, named
    for its subject — not one `sprint.md` everyone rewrites."""
    (in_repo / "specky.toml").write_text('[history]\nlong_lived = ["sprint"]\n')
    git(in_repo, "checkout", "-q", "-b", "sprint")
    mine = [commit(in_repo, "fix: one", ALICE), commit(in_repo, "fix: two", ALICE)]
    theirs = commit(in_repo, "fix: three", BOB)

    commit_doc.sync(assume_yes=True)

    docs = history(in_repo)
    assert "sprint.md" not in docs
    assert docs["one.md"].commits == mine
    assert docs["three.md"].commits == [theirs]


def test_a_commit_merged_into_a_feature_branch_is_not_folded_into_its_entry(in_repo, provider):
    main = default_branch(in_repo)
    git(in_repo, "checkout", "-q", "-b", "feat/other")
    other = commit(in_repo, "feat: other", BOB, name="other.txt")
    git(in_repo, "checkout", "-q", main)
    git(in_repo, "checkout", "-q", "-b", "feat/x")
    own = commit(in_repo, "feat: x", name="x.txt")
    git(in_repo, "merge", "-q", "--no-ff", "-m", "Merge feat/other", "feat/other")

    commit_doc.sync(assume_yes=True)

    docs = history(in_repo)
    assert docs["feat-x.md"].commits == [own]
    assert docs["other.md"].commits == [other]


# --- rewrites ------------------------------------------------------------------------------


def test_a_rebase_moves_every_commit_of_the_entry_and_keeps_its_name(in_repo, provider):
    main = default_branch(in_repo)
    git(in_repo, "checkout", "-q", "-b", "feat/x")
    old = [commit(in_repo, m) for m in ("feat: x", "wip")]
    commit_doc.sync(assume_yes=True)
    git(in_repo, "add", "-A")
    git(in_repo, "commit", "-q", "-m", commit_doc._AUTO_COMMIT_MARKER)
    git(in_repo, "checkout", "-q", main)
    commit(in_repo, "main moved on", BOB, name="main.txt")
    git(in_repo, "checkout", "-q", "feat/x")
    git(in_repo, "rebase", "-q", main)
    new = git(in_repo, "log", "--reverse", "--format=%H", "-n3", "HEAD").split()[:2]

    moved = commit_doc.apply_rewrites(in_repo, "".join(f"{o} {n}\n" for o, n in zip(old, new)))

    entry = paths.history_dir(in_repo) / "feat-x.md"
    assert moved == [(entry, entry)]
    assert history(in_repo)["feat-x.md"].commits == new
    assert commit_doc.pending_commits(in_repo, since=main) == []


def test_an_interactive_squash_leaves_one_commit_where_it_had_two(in_repo, provider):
    git(in_repo, "checkout", "-q", "-b", "feat/x")
    first = commit(in_repo, "feat: x")
    old = [commit(in_repo, "wip"), commit(in_repo, "fix typo")]
    commit_doc.sync(assume_yes=True)
    git(in_repo, "reset", "-q", "--soft", "HEAD~2")
    git(in_repo, "commit", "-q", "-m", "wip, squashed")
    squashed = git(in_repo, "rev-parse", "HEAD").strip()

    commit_doc.apply_rewrites(in_repo, "".join(f"{o} {squashed}\n" for o in old))

    assert history(in_repo)["feat-x.md"].commits == [first, squashed]


def test_main_follows_an_amend_of_the_entrys_last_commit(in_repo, provider, monkeypatch):
    git(in_repo, "checkout", "-q", "-b", "feat/x")
    first = commit(in_repo, "feat: x")
    last = commit(in_repo, "wip")
    commit_doc.sync(assume_yes=True)
    git(in_repo, "commit", "-q", "--amend", "-m", "wip, amended")
    amended = git(in_repo, "rev-parse", "HEAD").strip()
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{last} {amended}\n"))

    commit_doc.main(rewritten=True)

    assert history(in_repo)["feat-x.md"].commits == [first, amended]
    # In place: nothing was deleted, so the doc commit carries the entry and nothing else of it.
    changed = git(in_repo, "show", "--name-status", "--format=", "HEAD").split("\n")
    assert not any(line.startswith("D") for line in changed)


def test_a_commit_a_rebase_replays_is_left_for_post_rewrite(in_repo, provider):
    """git fires `post-commit` for every commit a rebase replays. Documenting them there paid for
    each twice and, on the rebase's detached HEAD, left an entry per commit beside the branch's
    entry that `post-rewrite` then moved onto the same shas."""
    git(in_repo, "checkout", "-q", "-b", "feat/x")
    replayed = commit(in_repo, "feat: x")
    (in_repo / ".git" / "rebase-merge").mkdir()

    commit_doc.main()

    assert replayed in [sha for sha, _ in commit_doc.pending_commits(in_repo)]
    assert not provider.prompts


# --- who reads entries ---------------------------------------------------------------------


def test_the_indexer_gives_every_member_the_entrys_words_and_searches_them_once(
    in_repo, provider, write_doc
):
    write_doc("billing/refund-limits.md", "# Refund Limits\n\nCaps.\n", {"type": "feature"})
    git(in_repo, "checkout", "-q", "-b", "feat/refunds")
    shas = [commit(in_repo, m) for m in ("feat: refund limits", "wip")]
    commit_doc.sync(assume_yes=True)
    entry = paths.history_dir(in_repo) / "feat-refunds.md"
    text = entry.read_text().replace("---\n\n#", "features: [specs/billing/refund-limits.md]\n---\n\n#", 1)
    entry.write_text(text)

    run_index(in_repo)

    conn = db.connect(in_repo)
    try:
        rows = dict(conn.execute("SELECT sha, headline FROM commits WHERE sha IN (?, ?)", shas))
        summaries = dict(conn.execute("SELECT sha, summary FROM commits_fts WHERE sha IN (?, ?)", shas))
    finally:
        conn.close()
    assert set(rows.values()) == {"Refunds are capped per plan"}
    assert summaries[shas[0]] and not summaries[shas[1]]

    # A feature page lists the entry once, not once per commit.
    [row] = catalog.commits_for_doc(in_repo, "specs/billing/refund-limits.md")
    assert row["history_path"] == "specs/history/feat-refunds.md"


def test_the_trailer_says_which_commits_a_doc_commit_documents():
    assert _documented_revs("d" * 40, ["p" * 40], "docs: x", [], "specs/history/", ["a" * 40, "b" * 40]) == [
        "a" * 40,
        "b" * 40,
    ]
    # An entry's name is not a commit, so without a trailer the doc commit's parent is used.
    assert _documented_revs(
        "d" * 40, ["p" * 40], commit_doc._AUTO_COMMIT_MARKER, ["specs/history/feat-x.md"], "specs/history/"
    ) == ["p" * 40]


def test_a_catch_up_pairs_every_commit_it_documented_with_the_feature_doc(in_repo, write_doc):
    """One doc-sync commit documenting two commits: the trailer pairs both with the feature doc.
    Before it, the entry's name paired nothing and the parent rule only the last one."""
    write_doc("billing/refunds.md", "# Refunds\n", {"type": "feature"})
    git(in_repo, "add", "-A")
    git(in_repo, "commit", "-q", "-m", "docs: the refunds doc")
    shas = []
    for name in ("refund_a.py", "refund_b.py"):
        (in_repo / name).write_text("x = 1\n")
        git(in_repo, "add", name)
        git(in_repo, "commit", "-q", "-m", f"feat: {name}")
        shas.append(git(in_repo, "rev-parse", "HEAD").strip())
    (paths.history_dir(in_repo)).mkdir(parents=True, exist_ok=True)
    (paths.history_dir(in_repo) / "feat-refunds.md").write_text(
        f"---\ncommits: [{', '.join(shas)}]\n---\n\n# Refunds\n\nWords.\n"
    )
    doc = in_repo / "specs" / "billing" / "refunds.md"
    doc.write_text(doc.read_text() + "\nMore.\n")
    git(in_repo, "add", "-A")
    git(
        in_repo, "commit", "-q", "-m", commit_doc._AUTO_COMMIT_MARKER,
        "-m", f"{commit_doc.DOCUMENTS_TRAILER}: {' '.join(shas)}",
    )

    run_index(in_repo)

    conn = db.connect(in_repo)
    try:
        paired = {p for (p,) in conn.execute("SELECT path FROM doc_files WHERE doc_path = ?", ("specs/billing/refunds.md",))}
    finally:
        conn.close()
    assert {"refund_a.py", "refund_b.py"} <= paired


# --- the document-commits skill ------------------------------------------------------------


def test_pending_json_says_which_entry_each_commit_joins(in_repo, provider):
    git(in_repo, "checkout", "-q", "-b", "feat/x")
    first = commit(in_repo, "feat: x")
    commit_doc.sync(assume_yes=True)
    second, third = commit(in_repo, "wip"), commit(in_repo, "fix typo")

    plan = commit_doc.entry_plan(in_repo, commit_doc.pending_commits(in_repo))

    assert plan == {second: "specs/history/feat-x.md", third: "specs/history/feat-x.md"}
    assert first not in plan  # documented already


def test_a_commit_joining_a_new_entry_names_the_commit_that_opens_it(in_repo):
    git(in_repo, "checkout", "-q", "-b", "feat/x")
    first, second = commit(in_repo, "feat: x"), commit(in_repo, "wip")

    plan = commit_doc.entry_plan(in_repo, [(first, ""), (second, "")])

    assert plan == {first: "", second: first}


def test_record_commit_extends_the_entry_the_plan_named(in_repo):
    git(in_repo, "checkout", "-q", "-b", "feat/x")
    first, second = commit(in_repo, "feat: x"), commit(in_repo, "wip")

    commit_doc.record_commit(in_repo, first, SUMMARY)
    path = commit_doc.record_commit(in_repo, second, SUMMARY.replace("capped per plan", "capped"))

    assert path.name == "feat-x.md"
    entry = history(in_repo)["feat-x.md"]
    assert entry.commits == [first, second]
    assert entry.headline == "Refunds are capped"
