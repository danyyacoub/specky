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
    monkeypatch.setattr(
        commit_doc, "load_provider_from_toml", lambda _path, _command="": provider
    )


def _history(repo: Path) -> set[str]:
    return {p.stem for p in (repo / "specs" / "history").glob("*.md")}


# --- which commits get picked -------------------------------------------------------------


def pending(repo: Path, **kwargs) -> list[str]:
    """Shas only — the subject each pair carries is asserted separately."""
    return [sha for sha, _subject in commit_doc.pending_commits(repo, **kwargs)]


def test_a_commit_that_already_has_a_history_file_is_skipped(tmp_repo):
    """A doc with no `sha:` frontmatter is one written before that was recorded — its filename is
    the only claim it makes, so the filename is what it's matched on."""
    sha = _commit(tmp_repo, "second")
    history = tmp_repo / "specs" / "history"
    history.mkdir(parents=True)
    (history / f"{sha[:8]}.md").write_text("# done\n")

    assert sha not in pending(tmp_repo)


def test_a_history_doc_is_matched_on_the_full_sha_it_records(tmp_repo):
    sha = _commit(tmp_repo, "second")
    history = tmp_repo / "specs" / "history"
    history.mkdir(parents=True)
    (history / f"{sha[:8]}.md").write_text(f"---\nsha: {sha}\n---\n\n# done\n")

    assert sha not in pending(tmp_repo)


def test_an_eight_hex_prefix_collision_no_longer_hides_a_commit(tmp_repo):
    """`done` used to be a set of 8-char filename stems, so an unrelated commit sharing those 8
    hex digits was silently treated as already documented — and never got a doc at all. Rare per
    pair, unavoidable on a repo with enough commits, and invisible when it happens."""
    sha = _commit(tmp_repo, "second")
    history = tmp_repo / "specs" / "history"
    history.mkdir(parents=True)
    impostor = sha[:8] + "0" * 32
    (history / f"{sha[:8]}.md").write_text(f"---\nsha: {impostor}\n---\n\n# some other commit\n")

    assert sha in pending(tmp_repo)


def test_a_colliding_commit_gets_its_own_longer_filename(tmp_repo, monkeypatch):
    """...and documenting it must not overwrite the doc that holds the other commit's summary."""
    commit = commit_doc.Commit(
        sha="abcdef12" + "3" * 32, author="a", date="2026-01-01", message="mine", diff=""
    )
    history = tmp_repo / "specs" / "history"
    history.mkdir(parents=True)
    taken = history / "abcdef12.md"
    taken.write_text(f"---\nsha: {'abcdef12' + '9' * 32}\n---\n\n# the other one\n")

    path = commit_doc.write_history_file(tmp_repo, commit, "summary")

    assert path.name == "abcdef123333.md"
    assert "the other one" in taken.read_text()
    assert commit_doc.history_doc_for(history, commit.sha) == path


def test_a_re_sync_overwrites_the_docs_own_commit_not_a_neighbour(tmp_repo):
    commit = commit_doc.Commit(
        sha="abcdef12" + "3" * 32, author="a", date="2026-01-01", message="mine", diff=""
    )
    first = commit_doc.write_history_file(tmp_repo, commit, "first summary")
    again = commit_doc.write_history_file(tmp_repo, commit, "second summary")

    assert again == first
    assert "second summary" in again.read_text()


def test_the_history_doc_records_the_full_sha_and_still_reads_short(tmp_repo):
    commit = commit_doc.Commit(
        sha="a" * 40, author="Dev <d@example.com>", date="2026-01-01", message="subject\n\nbody", diff=""
    )
    text = commit_doc.write_history_file(tmp_repo, commit, "It changed things.").read_text()

    assert text.startswith(f"---\nsha: {'a' * 40}\n---\n\n# Commit aaaaaaaa\n")
    assert "- **Message:** subject" in text  # only the subject line, as before


def test_all_branches_picks_up_a_commit_only_on_a_side_branch(tmp_repo):
    git(tmp_repo, "checkout", "-q", "-b", "side")
    side = _commit(tmp_repo, "on the side")
    git(tmp_repo, "checkout", "-q", "-")

    assert side not in pending(tmp_repo)
    assert side in pending(tmp_repo, all_branches=True)


def test_all_branches_still_honours_since(tmp_repo):
    base = git(tmp_repo, "rev-parse", "HEAD").strip()
    git(tmp_repo, "checkout", "-q", "-b", "side")
    side = _commit(tmp_repo, "on the side")
    git(tmp_repo, "checkout", "-q", "-")

    assert pending(tmp_repo, since=base, all_branches=True) == [side]
    assert pending(tmp_repo, since="2099-01-01", all_branches=True) == []


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


# --- the default range (bare sync) --------------------------------------------------------


def test_bare_sync_only_looks_at_the_newest_default_depth_commits(in_repo, monkeypatch):
    """No --since/--limit/--all-branches: only SYNC_DEFAULT_DEPTH commits are even inspected, so
    an old, never-documented commit outside that window is left alone rather than surfacing a
    confirmation prompt or a surprise bill."""
    monkeypatch.setattr(commit_doc, "SYNC_DEFAULT_DEPTH", 2)
    shas = [_commit(in_repo, f"commit {i}") for i in range(3)]  # + the fixture's initial commit
    _use_provider(monkeypatch, RoutingProvider())

    commit_doc.sync()

    assert _history(in_repo) == {sha[:8] for sha in shas[-2:]}


def test_an_explicit_range_flag_overrides_the_default_depth(in_repo, monkeypatch):
    monkeypatch.setattr(commit_doc, "SYNC_DEFAULT_DEPTH", 1)
    shas = [_commit(in_repo, f"commit {i}") for i in range(3)]  # + the fixture's initial commit
    _use_provider(monkeypatch, RoutingProvider())

    commit_doc.sync(limit=10)

    assert len(_history(in_repo)) == len(shas) + 1


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
        def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
            if "explodes" in prompt:
                raise RuntimeError("provider said no")
            return super().generate(prompt, prefix=prefix, task=task)

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


def test_a_non_utf8_file_in_the_diff_does_not_abort_the_run(in_repo, monkeypatch):
    """git only omits content it detects as binary. A file with no early NUL but non-UTF-8 bytes
    is emitted as text, so the diff has to be decoded leniently or one such file takes the whole
    run down — a ReportLab PDF (`%\\x93\\x8c\\x8b\\x9e` right after `%PDF-1.4`) is the common case.
    """
    (in_repo / "doc.pdf").write_bytes(b"%PDF-1.4\n%\x93\x8c\x8b\x9e ReportLab\n" + b"x" * 200)
    git(in_repo, "add", "-A")
    git(in_repo, "commit", "-q", "-m", "add a pdf git thinks is text")
    sha = git(in_repo, "rev-parse", "HEAD").strip()

    commit = commit_doc._commit_info(sha)
    assert "�" in commit.diff  # decoded, with the undecodable bytes replaced

    _use_provider(monkeypatch, RoutingProvider())
    commit_doc.sync()

    assert sha[:8] in _history(in_repo)  # the run reached this commit instead of dying on it


# --- cold start: bootstrapping from the code before walking commits ------------------------------


def _source(repo: Path, rel: str, body: str = "def run():\n    return 1\n") -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"add {rel}")


def test_a_cold_repo_bootstraps_before_walking_commits(in_repo, monkeypatch, capsys):
    """The whole point of the ordering: the commit walk must classify into the docs bootstrap
    wrote, not invent parallel ones — on a cold repo `ExistingDocs` is empty, which is exactly the
    case the classification prompt calls a defect."""
    import json

    _source(in_repo, "src/billing/refund.py")
    provider = RoutingProvider(
        discovery=json.dumps(
            {
                "product": {"what_it_is": "Refunds things."},
                "domains": [
                    {
                        "domain": "billing",
                        "topic": "refunds",
                        "purpose": "Issue refunds",
                        "type": "feature",
                        "tags": ["billing"],
                        "paths": ["src/billing/refund.py"],
                    }
                ],
            }
        ),
    )
    _use_provider(monkeypatch, provider)

    commit_doc.sync(assume_yes=True)

    assert (in_repo / "specs" / "billing" / "refunds.md").exists()
    assert "writing them from the code first" in capsys.readouterr().out


def test_a_warm_repo_never_bootstraps(in_repo, monkeypatch, capsys, write_doc):
    _source(in_repo, "src/billing/refund.py")
    write_doc("billing/refunds.md", "# Billing\n", {"type": "feature", "tags": ["billing"]})
    provider = RoutingProvider()
    _use_provider(monkeypatch, provider)

    commit_doc.sync(assume_yes=True)

    assert "writing them from the code first" not in capsys.readouterr().out
    assert not any(p.startswith("You are reading a codebase") for p in provider.prompts)


def test_no_bootstrap_skips_the_offer(in_repo, monkeypatch, capsys):
    _source(in_repo, "src/billing/refund.py")
    provider = RoutingProvider()
    _use_provider(monkeypatch, provider)

    commit_doc.sync(assume_yes=True, bootstrap=False)

    assert "writing them from the code first" not in capsys.readouterr().out


def test_bootstrapped_commits_get_history_only(in_repo, monkeypatch):
    """Those commits are already in the docs bootstrap just wrote from the working tree, so asking
    a model to update the same docs from the same diffs is redundant — and in practice came back as
    whole-body rewrites that `lost_content` refused, leaving pending drafts for correct docs."""
    import json

    _source(in_repo, "src/billing/refund.py")
    provider = RoutingProvider(
        discovery=json.dumps(
            {
                "product": {"what_it_is": "Refunds things."},
                "domains": [
                    {
                        "domain": "billing",
                        "topic": "refunds",
                        "purpose": "Issue refunds",
                        "type": "feature",
                        "tags": ["billing"],
                        "paths": ["src/billing/refund.py"],
                    }
                ],
            }
        ),
    )
    _use_provider(monkeypatch, provider)

    commit_doc.sync(assume_yes=True)

    assert _history(in_repo), "history entries are still written"
    assert not any("decide whether it changes user-facing" in p for p in provider.prompts), (
        "no commit should have been classified on the bootstrap run"
    )
    assert not (in_repo / ".specky" / "pending").exists()


def test_batch_fetches_every_summary_in_one_request(in_repo, monkeypatch):
    """The micro-doc is the one call in the commit path that batches cleanly — it depends on
    nothing but its own commit. Classification deliberately cannot."""
    shas = [_commit(in_repo, f"commit {i}") for i in range(5)]

    class Batching(RoutingProvider):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.batches = []

        def generate_batch(self, prompts, task: str = ""):
            self.batches.append(prompts)
            return {sha: "batched summary" for sha in prompts}

    provider = Batching()
    _use_provider(monkeypatch, provider)

    commit_doc.sync(assume_yes=True, batch=True, bootstrap=False)

    assert len(provider.batches) == 1, "one request for the whole run, not one per chunk of four"
    assert len(provider.batches[0]) == len(shas) + 1  # + the fixture's initial commit
    doc = next((in_repo / "specs" / "history").glob("*.md")).read_text()
    assert "batched summary" in doc


def test_batch_falls_back_when_the_provider_has_none(in_repo, monkeypatch, capsys):
    _commit(in_repo, "second")
    _use_provider(monkeypatch, RoutingProvider())

    commit_doc.sync(assume_yes=True, batch=True, bootstrap=False)

    assert _history(in_repo), "the commits are still documented"
    assert "--batch ignored" in capsys.readouterr().out


def test_each_kind_of_call_reuses_one_stable_prefix(in_repo, monkeypatch):
    """A run makes two kinds of call, each with its own prefix, and each prefix must be
    byte-identical across every commit — a prefix cache is a literal prefix match, so anything
    commit-specific leaking into it turns every call into a miss and the saving silently vanishes."""
    for i in range(3):
        _commit(in_repo, f"commit {i}")
    provider = RoutingProvider()
    _use_provider(monkeypatch, provider)

    commit_doc.sync(assume_yes=True, bootstrap=False)

    prefixes = [p for p in provider.prefixes if p]
    summary = [p for p in prefixes if p.startswith("Summarize what changed")]
    classify = [p for p in prefixes if p.startswith("You maintain a set of")]
    assert len(set(summary)) == 1 and len(summary) == 4  # 3 commits + the fixture's initial one
    assert len(set(classify)) == 1 and len(classify) == 4
    assert len(set(prefixes)) == 2, "and no third, per-commit prefix crept in"
