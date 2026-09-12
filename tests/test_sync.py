"""`specky sync` over a real repo: which commits it picks, what it asks before spending money,
and what it prints while it works.

Every test here injects the provider by patching `commit_doc.load_provider_from_toml`, so no
specky.toml is needed and no network call is possible.
"""

from pathlib import Path

import pytest

from specky import commit_doc

from conftest import RoutingProvider, git


def _commit(repo: Path, message: str) -> str:
    (repo / f"{len(list(repo.glob('*.txt')))}.txt").write_text(message)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def in_repo(tmp_repo: Path, monkeypatch) -> Path:
    """sync() resolves the repo from the process cwd, like the real CLI does."""
    monkeypatch.chdir(tmp_repo)
    return tmp_repo


def _use_provider(monkeypatch, provider) -> None:
    monkeypatch.setattr(commit_doc, "load_provider_from_toml", lambda _path: provider)


def _history(repo: Path) -> set[str]:
    return {p.stem for p in (repo / "specs" / "history").glob("*.md")}


# --- which commits get picked -------------------------------------------------------------


def pending(repo: Path, **kwargs) -> list[str]:
    """Shas only — the subject each pair carries is asserted separately."""
    return [sha for sha, _subject in commit_doc.pending_commits(repo, **kwargs)]


def test_a_commit_that_already_has_a_history_file_is_skipped(tmp_repo):
    sha = _commit(tmp_repo, "second")
    history = tmp_repo / "specs" / "history"
    history.mkdir(parents=True)
    (history / f"{sha[:8]}.md").write_text("# done\n")

    assert sha not in pending(tmp_repo)


def test_speckys_own_doc_sync_commits_are_never_documented(tmp_repo):
    """The hook refuses to document its own follow-up commit; a backfill has to agree, or every
    `specky sync` pays to document specky's paperwork."""
    auto = _commit(tmp_repo, commit_doc._AUTO_COMMIT_MARKER)
    assert auto not in pending(tmp_repo)


def test_pending_commits_are_oldest_first_with_their_subjects(tmp_repo):
    first = git(tmp_repo, "rev-parse", "HEAD").strip()
    second = _commit(tmp_repo, "second")
    assert commit_doc.pending_commits(tmp_repo) == [
        (first, "initial commit"),
        (second, "second"),
    ]


def test_since_accepts_a_revision(tmp_repo):
    base = git(tmp_repo, "rev-parse", "HEAD").strip()
    second = _commit(tmp_repo, "second")
    assert pending(tmp_repo, since=base) == [second]
    assert pending(tmp_repo, since="HEAD~1") == [second]


def test_since_accepts_a_date(tmp_repo):
    _commit(tmp_repo, "second")
    assert pending(tmp_repo, since="2 weeks ago")  # dated now, so inside the window
    assert pending(tmp_repo, since="2099-01-01") == []


def test_limit_caps_after_filtering_not_before(tmp_repo):
    """`--limit 1` means one commit processed. Capping the rev-list first would spend that
    budget on commits that already have a doc."""
    first = git(tmp_repo, "rev-parse", "HEAD").strip()
    history = tmp_repo / "specs" / "history"
    history.mkdir(parents=True)
    (history / f"{first[:8]}.md").write_text("# done\n")
    second = _commit(tmp_repo, "second")

    assert pending(tmp_repo, limit=1) == [second]


# --- spending money on purpose ------------------------------------------------------------


def test_dry_run_lists_the_commits_and_contacts_no_provider(in_repo, monkeypatch, capsys):
    _commit(in_repo, "add refunds")

    def no_calls(_path):
        raise AssertionError("--dry-run must not load a provider")

    monkeypatch.setattr(commit_doc, "load_provider_from_toml", no_calls)
    assert commit_doc.sync(dry_run=True) == []

    out = capsys.readouterr().out
    assert "2 commits to document, ~4-6 AI calls" in out
    assert "[2/2] " in out and "add refunds" in out
    assert not (in_repo / "specs" / "history").exists()


def test_a_large_backfill_refuses_to_run_unattended(in_repo, monkeypatch):
    monkeypatch.setattr(commit_doc, "SYNC_CONFIRM_THRESHOLD", 2)
    _commit(in_repo, "second")
    _use_provider(monkeypatch, RoutingProvider())

    # pytest's stdin isn't a terminal, which is also true of CI and of the git hook.
    with pytest.raises(RuntimeError, match="re-run with --yes"):
        commit_doc.sync()
    assert not (in_repo / "specs" / "history").exists()


def test_yes_skips_the_confirmation(in_repo, monkeypatch):
    monkeypatch.setattr(commit_doc, "SYNC_CONFIRM_THRESHOLD", 2)
    _commit(in_repo, "second")
    _use_provider(monkeypatch, RoutingProvider())

    assert commit_doc.sync(assume_yes=True)
    assert len(_history(in_repo)) == 2


def test_an_interactive_no_cancels_before_any_call(in_repo, monkeypatch, capsys):
    monkeypatch.setattr(commit_doc, "SYNC_CONFIRM_THRESHOLD", 2)
    monkeypatch.setattr(commit_doc.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    _commit(in_repo, "second")
    _use_provider(monkeypatch, RoutingProvider())

    with pytest.raises(RuntimeError, match="cancelled"):
        commit_doc.sync()
    assert not (in_repo / "specs" / "history").exists()


def test_an_interactive_yes_proceeds(in_repo, monkeypatch):
    monkeypatch.setattr(commit_doc, "SYNC_CONFIRM_THRESHOLD", 2)
    monkeypatch.setattr(commit_doc.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    _commit(in_repo, "second")
    _use_provider(monkeypatch, RoutingProvider())

    assert len(_history(in_repo)) == 0
    commit_doc.sync()
    assert len(_history(in_repo)) == 2


# --- doing the work ----------------------------------------------------------------------


def test_limit_processes_exactly_n_commits(in_repo, monkeypatch):
    for i in range(3):
        _commit(in_repo, f"commit {i}")
    _use_provider(monkeypatch, RoutingProvider())

    commit_doc.sync(limit=2)
    assert len(_history(in_repo)) == 2


def test_every_commit_in_every_batch_is_documented(in_repo, monkeypatch, capsys):
    """Summaries are fetched a batch at a time, so the loop has to cross batch boundaries —
    with SYNC_CONCURRENCY at 2 and 5 commits, that's three batches, the last one short."""
    monkeypatch.setattr(commit_doc, "SYNC_CONCURRENCY", 2)
    shas = [git(in_repo, "rev-parse", "HEAD").strip()]
    shas += [_commit(in_repo, f"commit {i}") for i in range(4)]
    _use_provider(monkeypatch, RoutingProvider())

    written = commit_doc.sync()
    assert _history(in_repo) == {sha[:8] for sha in shas}
    assert len(written) == 5

    out = capsys.readouterr().out
    # Progress is numbered and in commit order even though the calls fanned out.
    assert [line.split("]")[0] + "]" for line in out.splitlines() if line.startswith("[")] == [
        "[1/5]",
        "[2/5]",
        "[3/5]",
        "[4/5]",
        "[5/5]",
    ]
    assert "specky sync: wrote 5 files across 5 commits" in out


def test_one_failing_commit_does_not_abandon_the_rest(in_repo, monkeypatch, capsys):
    bad_sha = _commit(in_repo, "explodes")
    _commit(in_repo, "fine")

    class HalfBroken(RoutingProvider):
        def generate(self, prompt: str) -> str:
            if "explodes" in prompt:
                raise RuntimeError("provider said no")
            return super().generate(prompt)

    _use_provider(monkeypatch, HalfBroken())
    commit_doc.sync()

    assert bad_sha[:8] not in _history(in_repo)
    assert len(_history(in_repo)) == 2  # the other two commits still got theirs
    assert "skipped (provider said no)" in capsys.readouterr().out


def test_a_feature_doc_is_written_and_linked(in_repo, monkeypatch):
    _use_provider(
        monkeypatch,
        RoutingProvider(
            classification='{"skip": false, "domain": "billing", "topic": "refund-flow", '
            '"purpose": "Issue refunds", "type": "workflow", "tags": ["refunds"]}',
            doc="# Billing — Refund Flow\n\n## What It Does\nRefunds.\n",
        ),
    )
    written = commit_doc.sync()

    doc = in_repo / "specs" / "billing" / "refund-flow.md"
    assert doc in written
    assert "tags: [refunds]" in doc.read_text()
    assert "billing/refund-flow.md" in (in_repo / "specs" / "MODULES.md").read_text()


def test_nothing_to_do_says_so(in_repo, monkeypatch, capsys):
    _use_provider(monkeypatch, RoutingProvider())
    commit_doc.sync()
    capsys.readouterr()

    assert commit_doc.sync() == []
    assert "already up to date" in capsys.readouterr().out
