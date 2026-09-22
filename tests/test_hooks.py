"""The git hooks: where they're installed, and what a fire actually does.

The thing being tested is a shift in what a hook *is*. It used to document `HEAD`, which meant a
commit that arrived by any route git doesn't fire `post-commit` for — a merge, a rebase, a
cherry-pick, a colleague with no hook installed, a squash-merge on the forge — was never documented
and never would be, because the next fire looked at `HEAD` too. Now every fire reconciles the tail of
`pending_commits()`, so a hook only has to fire *eventually*, which is the only property git gives us.

Two failure modes get their own tests because both are silent in production: a hook written to a
directory git doesn't read (`core.hooksPath`, a linked worktree), and a hook that runs from a PATH
without `specky` in it — which is every GUI git client.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from specky import commit_doc, paths
from specky.commit_doc import HOOK_MARKER, HOOKS, hooks_dir, install_git_hook
from specky.lock import exclusive

from conftest import RoutingProvider, git


def _commit(repo: Path, message: str) -> str:
    (repo / f"{len(list(repo.glob('*.txt')))}.txt").write_text(message)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def _use_provider(monkeypatch, provider) -> None:
    monkeypatch.setattr(
        commit_doc, "load_provider_from_toml", lambda _path, _command="": provider
    )


def _history(repo: Path) -> set[str]:
    return {p.stem for p in paths.history_dir(repo).glob("*.md")}


def _reject_commits(repo: Path) -> None:
    """A `pre-commit` hook that turns down every commit, as a lint gate does for a failing one."""
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)


class _SplicingProvider(RoutingProvider):
    """A provider that answers the section-update prompt with a `{"sections": ...}` envelope.

    `RoutingProvider` routes on "Respond with ONLY a JSON object", which the section-update prompt
    carries as well as the classification one — so a plain one answers an update with its
    classification, the doc is read as a whole-body replacement, and the content-loss gate refuses
    it. Splitting on the section list is what lets a test exercise an actual splice.
    """

    def __init__(self, sections: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._sections = sections

    def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
        if "Sections in the doc right now:" not in prompt:
            return super().generate(prompt, prefix=prefix, task=task)
        with self._lock:
            self.prompts.append(prompt)
        return self._sections


@pytest.fixture
def in_repo(tmp_repo: Path, monkeypatch) -> Path:
    """The hook entry point resolves the repo from the process cwd, as a real hook fire does."""
    monkeypatch.chdir(tmp_repo)
    return tmp_repo


class TestInstall:
    def test_installs_every_hook_git_can_fire(self, in_repo: Path):
        written = install_git_hook()

        assert [p.name for p in written] == list(HOOKS)
        for path in written:
            assert HOOK_MARKER in path.read_text()
            assert path.stat().st_mode & stat.S_IXUSR

    def test_post_rewrite_is_the_only_one_passing_rewritten(self, in_repo: Path):
        installed = {p.name: p.read_text() for p in install_git_hook()}

        assert "--rewritten" in installed["post-rewrite"]
        assert "--rewritten" not in installed["post-commit"]
        assert "--rewritten" not in installed["post-merge"]

    def test_honours_core_hookspath(self, in_repo: Path):
        # pre-commit, husky and lefthook all set this. A hook written to `.git/hooks` in such a repo
        # is never run by git, which is indistinguishable from specky being broken.
        git(in_repo, "config", "core.hooksPath", ".githooks")

        written = install_git_hook()

        assert hooks_dir(in_repo) == in_repo / ".githooks"
        assert all(p.parent == in_repo / ".githooks" for p in written)
        assert not (in_repo / ".git/hooks/post-commit").exists()

    def test_an_absolute_hookspath_is_used_as_given(self, in_repo: Path, tmp_path: Path):
        shared = tmp_path / "shared-hooks"
        git(in_repo, "config", "core.hooksPath", str(shared))

        written = install_git_hook()

        assert all(p.parent == shared for p in written)

    def test_works_inside_a_linked_worktree(self, in_repo: Path, tmp_path: Path, monkeypatch):
        # In a linked worktree `.git` is a *file*, so `repo_root / ".git" / "hooks"` is not a
        # directory and the old install wrote three files into a path git never reads.
        _commit(in_repo, "something to branch from")
        linked = tmp_path / "linked"
        git(in_repo, "worktree", "add", "-q", str(linked), "-b", "side")
        monkeypatch.chdir(linked)

        written = install_git_hook()

        assert (linked / ".git").is_file()
        # Hooks live in the common dir, shared by every worktree — which is what we want: a commit
        # made in any worktree should reconcile the same docs tree.
        assert all(p.exists() and p.parent.name == "hooks" for p in written)
        assert git(linked, "rev-parse", "--git-common-dir").strip() in str(written[0])

    def test_a_foreign_hook_stops_the_whole_install(self, in_repo: Path):
        hooks = in_repo / ".git/hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "post-merge").write_text("#!/bin/sh\necho someone elses hook\n")

        with pytest.raises(RuntimeError, match="wasn't installed by specky"):
            install_git_hook()

        # All-or-nothing: a half-installed set is the state that's hardest to reason about later.
        assert not (hooks / "post-commit").exists()
        assert "someone elses hook" in (hooks / "post-merge").read_text()

    def test_reinstalling_over_speckys_own_hooks_is_fine(self, in_repo: Path):
        install_git_hook()
        assert [p.name for p in install_git_hook()] == list(HOOKS)


class TestHookScript:
    """The installed shell script, run the way git runs it."""

    @pytest.fixture
    def stub_specky(self, in_repo: Path, tmp_path: Path, monkeypatch) -> Path:
        """A `specky` that records the arguments it was called with, at an absolute path."""
        marker = tmp_path / "called.txt"
        stub = tmp_path / "bin" / "specky"
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text(f'#!/bin/sh\necho "$@" >> "{marker}"\ncat >/dev/null 2>&1 || true\n')
        stub.chmod(0o755)
        monkeypatch.setattr(commit_doc.shutil, "which", lambda _name: str(stub))
        install_git_hook()
        return marker

    def _run_hook(self, repo: Path, name: str, path_value: str) -> subprocess.CompletedProcess:
        hook = repo / ".git/hooks" / name
        # `env -i`-style: the sanitized environment a GUI git client (IntelliJ, Fork, Tower) hands
        # its hooks, which is where `command -v specky` comes up empty.
        return subprocess.run(
            ["/bin/sh", str(hook)],
            cwd=repo,
            env={"PATH": path_value, "HOME": os.environ.get("HOME", "")},
            input="",
            capture_output=True,
            text=True,
        )

    def test_falls_back_to_an_absolute_path_when_specky_is_not_on_path(
        self, in_repo: Path, stub_specky: Path
    ):
        result = self._run_hook(in_repo, "post-commit", "/usr/bin:/bin")

        assert result.returncode == 0
        # The old script was `command -v specky … || true`, which in this shell is a silent no-op —
        # the single most common reason a repo has the hook installed and no docs to show for it.
        assert stub_specky.read_text().strip() == "commit-doc"

    def test_post_rewrite_passes_the_flag_through_the_fallback(
        self, in_repo: Path, stub_specky: Path
    ):
        self._run_hook(in_repo, "post-rewrite", "/usr/bin:/bin")

        assert stub_specky.read_text().strip() == "commit-doc --rewritten"

    def test_a_failing_specky_never_fails_the_hook(self, in_repo: Path, tmp_path: Path, monkeypatch):
        stub = tmp_path / "specky"
        stub.write_text("#!/bin/sh\nexit 3\n")
        stub.chmod(0o755)
        monkeypatch.setattr(commit_doc.shutil, "which", lambda _name: str(stub))
        install_git_hook()

        # git aborts nothing on a post-commit failure, but a non-zero hook is noise in every commit
        # and a broken build in some clients.
        assert self._run_hook(in_repo, "post-commit", "/usr/bin:/bin").returncode == 0



PLUGIN_HOOK = Path(__file__).resolve().parents[1] / "hooks" / "post-tool-use-commit.sh"


class TestPluginHook:
    """Claude Code's PostToolUse hook, which the plugin runs after *every* Bash call in *every* repo.

    The plugin is enabled per user, so most repos it fires in have never heard of specky. Acting
    there would mean a config error printed after every commit and an untracked `.specky/` left
    behind, so the hook waits for the `specky.toml` that `specky init` writes.
    """

    @pytest.fixture
    def stub_on_path(self, tmp_path: Path) -> tuple[Path, str]:
        marker = tmp_path / "called.txt"
        bin_dir = tmp_path / "plugin-bin"
        bin_dir.mkdir()
        stub = bin_dir / "specky"
        stub.write_text(f'#!/bin/sh\necho "$@" >> "{marker}"\n')
        stub.chmod(0o755)
        return marker, f"{bin_dir}:/usr/bin:/bin"

    def _fire(self, repo: Path, command: str, path_value: str) -> subprocess.CompletedProcess:
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        return subprocess.run(
            ["/bin/sh", str(PLUGIN_HOOK)],
            cwd=repo,
            env={"PATH": path_value, "HOME": os.environ.get("HOME", "")},
            input=payload,
            capture_output=True,
            text=True,
        )

    def test_does_nothing_in_a_repo_without_specky_toml(self, tmp_repo: Path, stub_on_path):
        marker, path_value = stub_on_path

        result = self._fire(tmp_repo, 'git commit -m "x"', path_value)

        assert result.returncode == 0
        assert result.stdout == ""
        assert not marker.exists()
        assert not (tmp_repo / ".specky").exists()

    def test_runs_commit_doc_in_an_opted_in_repo(self, tmp_repo: Path, stub_on_path):
        marker, path_value = stub_on_path
        (tmp_repo / "specky.toml").write_text("[ai]\n")

        result = self._fire(tmp_repo, 'git add -A && git commit -m "x"', path_value)

        assert result.returncode == 0
        assert marker.read_text().strip() == "commit-doc"

    def test_ignores_bash_calls_that_are_not_commits(self, tmp_repo: Path, stub_on_path):
        marker, path_value = stub_on_path
        (tmp_repo / "specky.toml").write_text("[ai]\n")

        self._fire(tmp_repo, "git status", path_value)

        assert not marker.exists()

    def test_outside_a_git_repo_it_exits_cleanly(self, tmp_path: Path, stub_on_path):
        marker, path_value = stub_on_path
        elsewhere = tmp_path / "not-a-repo"
        elsewhere.mkdir()

        result = self._fire(elsewhere, 'git commit -m "x"', path_value)

        assert result.returncode == 0
        assert not marker.exists()


class TestCatchUp:
    def test_a_fire_documents_the_commits_earlier_fires_missed(self, in_repo: Path, monkeypatch):
        """The core of the reconciliation change.

        These three commits stand in for anything git doesn't fire `post-commit` for — a merge, a
        rebase, a cherry-pick, or a colleague who never ran `install-git-hook`. Documenting `HEAD`
        would leave the first two undocumented forever.
        """
        _use_provider(monkeypatch, RoutingProvider())
        shas = [_commit(in_repo, f"commit {i}") for i in range(3)]

        commit_doc.main()

        assert {sha[:8] for sha in shas} <= _history(in_repo)

    def test_a_fire_spends_no_more_than_the_catch_up_cap(self, in_repo: Path, monkeypatch, capsys):
        # A `git pull` that fast-forwards 300 undocumented commits fires post-merge once, and that
        # one fire must not become 600 provider calls inside a git hook.
        _use_provider(monkeypatch, RoutingProvider())
        for i in range(commit_doc.HOOK_CATCHUP_MAX + 3):
            _commit(in_repo, f"commit {i}")

        commit_doc.main()

        assert len(_history(in_repo)) == commit_doc.HOOK_CATCHUP_MAX
        assert "still undocumented — run `specky sync`" in capsys.readouterr().out

    def test_the_disable_env_var_stops_a_fire_before_it_spends_anything(
        self, in_repo: Path, monkeypatch, capsys
    ):
        """The opt-out for a checkout that didn't choose its own hooks: a repo that commits them and
        sets `core.hooksPath`, or a cloud agent's VM whose commits belong in a pull request rather
        than in a doc-sync commit nobody asked for. Checked before the provider is even built."""
        _use_provider(
            monkeypatch, RoutingProvider()
        )  # would document three commits if it got that far
        for i in range(3):
            _commit(in_repo, f"commit {i}")
        monkeypatch.setenv(commit_doc.DISABLE_HOOK_ENV, "1")

        commit_doc.main()

        assert _history(in_repo) == set()
        # One line, not silence: "the hook is installed and no docs appear" is specky's hardest
        # failure to diagnose, so the reason lands in the same output the commit did.
        assert commit_doc.DISABLE_HOOK_ENV in capsys.readouterr().out

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "FALSE"])
    def test_values_that_read_as_off_leave_the_hook_alone(
        self, in_repo: Path, monkeypatch, value: str
    ):
        """`SPECKY_DISABLE_HOOK=0` is the obvious way to write "no", and a bare `is set` test would
        make it mean yes."""
        _use_provider(monkeypatch, RoutingProvider())
        sha = _commit(in_repo, "a commit")
        monkeypatch.setenv(commit_doc.DISABLE_HOOK_ENV, value)

        commit_doc.main()

        assert sha[:8] in _history(in_repo)

    def test_the_backlog_walk_is_bounded_by_depth_not_by_repo_size(self, in_repo: Path):
        for i in range(5):
            _commit(in_repo, f"commit {i}")

        assert len(commit_doc.pending_commits(in_repo, depth=2)) == 2
        # Unlike a `HEAD~20` revision, a depth is safe on a repo with fewer commits than that.
        assert len(commit_doc.pending_commits(in_repo, depth=500)) == 6

    def test_its_own_doc_sync_commit_does_not_start_another_round(self, in_repo: Path, monkeypatch):
        provider = RoutingProvider()
        _use_provider(monkeypatch, provider)
        _commit(in_repo, "real work")
        commit_doc.main()
        assert git(in_repo, "log", "-1", "--format=%s").strip().startswith(
            "docs: sync specky docs"
        )
        calls = len(provider.prompts)

        commit_doc.main()  # the fire caused by the doc-sync commit itself

        assert len(provider.prompts) == calls

    def test_a_second_run_while_one_holds_the_lock_writes_nothing(self, in_repo: Path, monkeypatch, capsys):
        provider = RoutingProvider()
        _use_provider(monkeypatch, provider)
        _commit(in_repo, "real work")

        with exclusive(in_repo):
            commit_doc.main()

        assert provider.prompts == []
        assert _history(in_repo) == set()
        assert "another specky run is writing docs" in capsys.readouterr().out

    def test_a_broken_provider_never_breaks_the_commit(self, in_repo: Path, monkeypatch, capsys):
        def explode(_path, _command=""):
            raise RuntimeError("provider is down")

        monkeypatch.setattr(commit_doc, "load_provider_from_toml", explode)
        _commit(in_repo, "real work")

        commit_doc.main()  # must not raise: the commit has already landed

        assert "failed, commit is unaffected" in capsys.readouterr().out


class TestTheFollowUpCommit:
    def test_only_the_docs_this_run_wrote_are_committed(self, in_repo: Path, monkeypatch):
        """A bare `git add <docs root>` swept up whatever else was in the tree.

        Somebody mid-sentence in a feature doc when a commit lands would find their draft committed
        under specky's name — and reverting the bot's commit would take their work with it.
        """
        _use_provider(monkeypatch, RoutingProvider())
        draft = in_repo / "specs" / "billing" / "half-written.md"
        draft.parent.mkdir(parents=True)
        draft.write_text("# Refunds\n\nI was in the middle of a sen\n")
        _commit(in_repo, "real work")  # commits the draft as it stands
        draft.write_text("# Refunds\n\nI was in the middle of a sentence when a commit landed.\n")

        commit_doc.main()

        committed = git(in_repo, "show", "--name-only", "--format=", "HEAD").split()
        assert "specs/billing/half-written.md" not in committed
        assert "sentence when a commit landed" in draft.read_text()
        assert " M specs/billing/half-written.md" in git(in_repo, "status", "--porcelain")

    def test_an_uncommitted_edit_rides_along_in_a_doc_this_fire_rewrites(
        self, in_repo: Path, monkeypatch
    ):
        """The limit of the protection above: it keeps whole *files* out, not *edits*.

        `git add -- <path>` stages that file's entire working-tree content, so a doc the fire
        rewrites is committed as it stands — the human's uncommitted edits in it included, under
        specky's marker message. Observed on a trial repo where `specky tag` had left frontmatter
        uncommitted in 43 docs: the three the next fire updated had that frontmatter committed for
        them, and the rest stayed dirty. Asserted rather than fixed, because git offers nothing
        narrower to stage; see the doc's "What Staging By Path Cannot Do".
        """
        _use_provider(
            monkeypatch,
            _SplicingProvider(
                # Only `## What It Does`, so the splice carries the section the human was editing
                # through untouched — a whole-body answer would replace it and never reach the
                # commit, which would test the content-loss gate instead of this.
                sections='{"sections": {"What It Does": "Refunds, restated by specky.\\n"}}',
                classification='{"skip": false, "domain": "billing", "topic": "refund-flow", '
                '"purpose": "Issue refunds", "type": "workflow", "tags": ["refunds"]}',
            ),
        )
        rewritten = in_repo / "specs" / "billing" / "refund-flow.md"
        untouched = in_repo / "specs" / "search" / "indexing.md"
        for doc in (rewritten, untouched):
            doc.parent.mkdir(parents=True)
            doc.write_text("# Doc\n\n## What It Does\nThings.\n\n## How It Works\nSomehow.\n")
        _commit(in_repo, "real work")  # both docs land committed and clean

        for doc in (rewritten, untouched):
            doc.write_text(doc.read_text().replace("Somehow.", "Somehow, and a human said so.\n"))

        commit_doc.main()

        committed = git(in_repo, "show", "--name-only", "--format=", "HEAD").split()
        assert "specs/billing/refund-flow.md" in committed
        # The human's sentence is in specky's commit, not merely still on disk.
        assert "a human said so" in git(in_repo, "show", "HEAD:specs/billing/refund-flow.md")

        assert "specs/search/indexing.md" not in committed
        assert " M specs/search/indexing.md" in git(in_repo, "status", "--porcelain")

    def test_unrelated_staged_work_stays_staged(self, in_repo: Path, monkeypatch):
        _use_provider(monkeypatch, RoutingProvider())
        _commit(in_repo, "real work")
        (in_repo / "src.py").write_text("def next_thing(): ...\n")
        git(in_repo, "add", "src.py")

        commit_doc.main()

        assert "A  src.py" in git(in_repo, "status", "--porcelain")

    @pytest.mark.parametrize("state", ["CHERRY_PICK_HEAD", "REVERT_HEAD", "MERGE_HEAD"])
    def test_docs_are_written_but_not_committed_mid_sequencer(
        self, in_repo: Path, monkeypatch, capsys, state: str
    ):
        # A `git commit` during a rebase or cherry-pick lands in the middle of someone else's
        # replay, which at best confuses the sequencer and at worst has to be untangled by hand.
        _use_provider(monkeypatch, RoutingProvider())
        sha = _commit(in_repo, "real work")
        (in_repo / ".git" / state).write_text(f"{sha}\n")

        commit_doc.main()

        assert sha[:8] in _history(in_repo)  # written…
        assert git(in_repo, "log", "-1", "--format=%s").strip() == "real work"  # …not committed
        out = capsys.readouterr().out
        assert "left uncommitted" in out and state in out

    def test_a_rejected_doc_commit_leaves_nothing_staged(self, in_repo: Path, monkeypatch, capsys):
        """A pre-commit gate that rejects the doc commit must not cost the developer their next one.

        The docs are staged into the real index before the commit, so a rejection used to leave them
        there — and the developer's next `git commit` swept specky's docs into their feature commit
        under their name. Reproduced against the pre-commit framework's `end-of-file-fixer`, which
        matches `.md` and so hits every doc commit on a repo that runs it.
        """
        _use_provider(monkeypatch, RoutingProvider())
        _commit(in_repo, "real work")  # lands first: the gate below would reject this one too
        _reject_commits(in_repo)

        commit_doc.main()

        assert "not committed" in capsys.readouterr().out
        assert _history(in_repo)  # the docs are on disk, waiting for the ledger's retry…
        assert git(in_repo, "diff", "--cached", "--name-only") == ""  # …and not in anyone's index

    def test_a_rejected_doc_commit_keeps_the_humans_own_staged_work(
        self, in_repo: Path, monkeypatch
    ):
        # The un-staging is scoped to what specky staged: a rollback wide enough to catch a path the
        # human had staged themselves would be its own version of the same theft.
        _use_provider(monkeypatch, RoutingProvider())
        _commit(in_repo, "real work")
        _reject_commits(in_repo)
        (in_repo / "src.py").write_text("def next_thing(): ...\n")
        git(in_repo, "add", "src.py")

        commit_doc.main()

        assert "A  src.py" in git(in_repo, "status", "--porcelain")

    def test_a_byte_for_byte_identical_regeneration_makes_no_commit(self, in_repo: Path, monkeypatch):
        _use_provider(monkeypatch, RoutingProvider())
        _commit(in_repo, "real work")
        commit_doc.main()
        head = git(in_repo, "rev-parse", "HEAD").strip()

        commit_doc.main()

        assert git(in_repo, "rev-parse", "HEAD").strip() == head


class TestRewrites:
    """`post-rewrite`: an amend or a rebase replaces a sha, and the doc follows it."""

    def _documented(self, repo: Path, monkeypatch) -> tuple[str, Path]:
        """A repo with every commit documented, and HEAD still the commit under test.

        `sync()` rather than `main()`: main's follow-up doc-sync commit would become HEAD, and the
        `git commit --amend` each test then runs would rewrite *that* instead of the work commit.
        """
        _use_provider(monkeypatch, RoutingProvider(summary="The summary a human then edited."))
        sha = _commit(repo, "original message")
        commit_doc.sync()
        return sha, paths.history_dir(repo) / f"{sha[:8]}.md"

    def test_an_amend_renames_the_doc_instead_of_orphaning_it(self, in_repo: Path, monkeypatch):
        old_sha, old_doc = self._documented(in_repo, monkeypatch)
        assert old_doc.exists()
        git(in_repo, "commit", "-q", "--amend", "-m", "amended message")
        new_sha = git(in_repo, "rev-parse", "HEAD").strip()

        moved = commit_doc.apply_rewrites(in_repo, f"{old_sha} {new_sha}\n")

        assert not old_doc.exists()
        assert [(old.name, new.name) for old, new in moved] == [
            (f"{old_sha[:8]}.md", f"{new_sha[:8]}.md")
        ]
        text = moved[0][1].read_text()
        assert new_sha in text
        assert "amended message" in text
        # The summary is read back off disk, so a hand-edited one survives the rename — and no
        # provider call is made to re-say what the old doc already said.
        assert "The summary a human then edited." in text

    def test_a_rename_costs_no_provider_call(self, in_repo: Path, monkeypatch):
        old_sha, _ = self._documented(in_repo, monkeypatch)
        git(in_repo, "commit", "-q", "--amend", "-m", "amended message")
        new_sha = git(in_repo, "rev-parse", "HEAD").strip()

        class Forbidden:
            def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
                raise AssertionError("a rewrite must not call the provider")

        _use_provider(monkeypatch, Forbidden())
        assert commit_doc.apply_rewrites(in_repo, f"{old_sha} {new_sha}\n")

    def test_the_renamed_commit_is_no_longer_pending(self, in_repo: Path, monkeypatch):
        old_sha, _ = self._documented(in_repo, monkeypatch)
        git(in_repo, "commit", "-q", "--amend", "-m", "amended message")
        new_sha = git(in_repo, "rev-parse", "HEAD").strip()

        commit_doc.apply_rewrites(in_repo, f"{old_sha} {new_sha}\n")

        # The point of the rename: without it the new sha looks undocumented and the next fire pays
        # to write almost exactly the same paragraph again.
        assert new_sha not in [sha for sha, _ in commit_doc.pending_commits(in_repo)]

    def test_the_index_rows_follow_the_new_sha(self, in_repo: Path, monkeypatch):
        from specky.db import connect

        old_sha, _ = self._documented(in_repo, monkeypatch)
        git(in_repo, "commit", "-q", "--amend", "-m", "amended message")
        new_sha = git(in_repo, "rev-parse", "HEAD").strip()

        commit_doc.apply_rewrites(in_repo, f"{old_sha} {new_sha}\n")

        conn = connect(in_repo)
        try:
            shas = [r[0] for r in conn.execute("SELECT sha FROM micro_docs").fetchall()]
        finally:
            conn.close()
        assert new_sha in shas and old_sha not in shas

    def test_a_pair_with_no_doc_is_left_to_the_ordinary_catch_up(self, in_repo: Path, monkeypatch):
        _use_provider(monkeypatch, RoutingProvider())
        sha = _commit(in_repo, "never documented")

        assert commit_doc.apply_rewrites(in_repo, f"{'0' * 40} {sha}\n") == []
        assert sha in [s for s, _ in commit_doc.pending_commits(in_repo)]

    @pytest.mark.parametrize(
        "line", ["", "   ", "onlyonefield", "short abc", "not-a-sha-pair-at-all"]
    )
    def test_malformed_stdin_is_skipped_rather_than_raising(self, in_repo: Path, line: str):
        # This runs in a hook: a line git wrote in a shape we didn't expect must not become a
        # traceback in the middle of somebody's rebase.
        assert commit_doc._rewrite_pairs(line) == []

    def test_an_interactive_squashes_third_field_is_ignored(self, in_repo: Path):
        old, new = "a" * 40, "b" * 40
        assert commit_doc._rewrite_pairs(f"{old} {new} squash\n") == [(old, new)]

    def test_main_reads_the_pairs_off_stdin(self, in_repo: Path, monkeypatch):
        old_sha, old_doc = self._documented(in_repo, monkeypatch)
        git(in_repo, "commit", "-q", "--amend", "-m", "amended message")
        new_sha = git(in_repo, "rev-parse", "HEAD").strip()
        monkeypatch.setattr("sys.stdin", io.StringIO(f"{old_sha} {new_sha}\n"))

        commit_doc.main(rewritten=True)

        assert not old_doc.exists()
        assert f"{new_sha[:8]}" in _history(in_repo)

    def test_an_uncommitted_old_doc_does_not_break_the_follow_up_commit(
        self, in_repo: Path, monkeypatch, capsys
    ):
        # `sync()` leaves its docs uncommitted, so the doc the rename deletes here was never tracked.
        # Handing git the deletion of a path it never knew fails with `pathspec did not match any
        # files` — and takes the whole follow-up commit down with it.
        old_sha, _ = self._documented(in_repo, monkeypatch)
        git(in_repo, "commit", "-q", "--amend", "-m", "amended message")
        new_sha = git(in_repo, "rev-parse", "HEAD").strip()
        monkeypatch.setattr("sys.stdin", io.StringIO(f"{old_sha} {new_sha}\n"))

        commit_doc.main(rewritten=True)

        assert "not committed" not in capsys.readouterr().out
        committed = git(in_repo, "show", "--name-only", "--format=", "HEAD")
        assert f"specs/history/{new_sha[:8]}.md" in committed


class TestARenameIsCommitted:
    """A rename left in the working tree is worse than no rename at all.

    The deleted old-sha doc is still committed, so the next `git checkout` or merge brings it back —
    now an orphan describing a commit that isn't in the history — while the new-sha doc stays
    untracked, so a clean clone (and therefore CI) still sees that commit as undocumented and pays to
    document it again.
    """

    def _replayed(self, repo: Path, monkeypatch) -> tuple[str, str]:
        """A repo where a commit has been replaced by a copy with a different sha, as a rebase does.

        The replay covers specky's own doc-sync commit too — which is what makes the old-sha doc a
        tracked file at the point the rename happens, and HEAD a marker commit.
        """
        _use_provider(monkeypatch, RoutingProvider())
        base = git(repo, "rev-parse", "HEAD").strip()
        old_sha = _commit(repo, "work that gets replayed")
        commit_doc.main()  # documents it *and* commits the doc
        doc_commit = git(repo, "rev-parse", "HEAD").strip()

        git(repo, "reset", "-q", "--hard", base)
        (repo / "moved-on.md").write_text("what the branch is replayed onto\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "main moved on")  # or the replay reproduces the same sha
        git(repo, "cherry-pick", old_sha, doc_commit)
        return old_sha, git(repo, "rev-parse", "HEAD~1").strip()

    def test_both_halves_of_the_rename_land_in_one_commit(self, in_repo: Path, monkeypatch):
        old_sha, new_sha = self._replayed(in_repo, monkeypatch)
        monkeypatch.setattr("sys.stdin", io.StringIO(f"{old_sha} {new_sha}\n"))

        commit_doc.main(rewritten=True)

        assert git(in_repo, "status", "--porcelain", "--", "specs") == ""
        # Committed, and committed as a *rename* — the old path is gone from the tree rather than
        # only from the working copy, which is what stops the next checkout resurrecting it.
        tracked = git(in_repo, "ls-tree", "-r", "--name-only", "HEAD")
        assert f"specs/history/{old_sha[:8]}.md" not in tracked
        assert f"specs/history/{new_sha[:8]}.md" in tracked

    def test_a_marker_head_no_longer_stops_the_rename_being_committed(
        self, in_repo: Path, monkeypatch
    ):
        # A rebase replays specky's own doc-sync commit, so `post-rewrite` fires with HEAD's subject
        # already carrying the marker. Returning there — which is what the recursion guard used to
        # do — is what left the rename in the tree for good.
        old_sha, new_sha = self._replayed(in_repo, monkeypatch)
        assert git(in_repo, "log", "-1", "--format=%s").strip().startswith(
            commit_doc._AUTO_COMMIT_MARKER
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(f"{old_sha} {new_sha}\n"))

        commit_doc.main(rewritten=True)

        assert f"{new_sha[:8]}" in _history(in_repo)
        assert git(in_repo, "status", "--porcelain", "--", "specs") == ""

    def test_a_rename_deferred_by_a_sequencer_is_committed_by_the_next_fire(
        self, in_repo: Path, monkeypatch, capsys
    ):
        # `post-rewrite` fires while `rebase-merge` still exists, so a rebase's rename can never be
        # committed by the fire that made it. The ledger is how the promise in that printed line —
        # "committed by the next fire" — is actually kept.
        old_sha, new_sha = self._replayed(in_repo, monkeypatch)
        monkeypatch.setattr("sys.stdin", io.StringIO(f"{old_sha} {new_sha}\n"))
        (in_repo / ".git" / "CHERRY_PICK_HEAD").write_text(f"{new_sha}\n")

        commit_doc.main(rewritten=True)

        ledger = in_repo / ".specky" / commit_doc.DEFERRED_LEDGER
        assert "left uncommitted" in capsys.readouterr().out
        assert f"specs/history/{old_sha[:8]}.md" in ledger.read_text()
        assert git(in_repo, "status", "--porcelain", "--", "specs") != ""

        (in_repo / ".git" / "CHERRY_PICK_HEAD").unlink()
        commit_doc.main()  # the next ordinary fire

        assert git(in_repo, "status", "--porcelain", "--", "specs") == ""
        assert not ledger.exists()  # and it isn't carried around forever after that

    def test_nothing_owed_and_nothing_pending_takes_no_lock(self, in_repo: Path, monkeypatch):
        # The cheap path has to stay cheap: every commit fires three chances to reach this, and one
        # that grabbed the lock would make the nested fire of its own doc-sync commit print a
        # spurious "another specky run is writing docs" line.
        _use_provider(monkeypatch, RoutingProvider())
        _commit(in_repo, "real work")
        commit_doc.main()

        def refuse(_repo_root):
            raise AssertionError("a fire with nothing to do must not take the lock")

        monkeypatch.setattr(commit_doc, "exclusive", refuse)
        commit_doc.main()
