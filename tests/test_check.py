"""`specky check` — the CI gate, and the `doc_files` table it reads.

Every test here builds the link the way specky's post-commit hook does, in git: a commit touches
code, and the hook's follow-up `docs: sync specky docs [skip specky]` commit adds a
`specs/history/<sha>.md` naming it plus whatever feature doc it produced. `specky index` derives
`doc_files` from that, and `check` is a lookup against it — no AI, no history walk, and nothing
read from state a fresh clone wouldn't have.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from specky import check, cli, db, indexer
from specky.check import CheckConfig, Report, Violation, resolve_base, run_check
from specky.indexer import run_index

from conftest import git

DOC = "specs/billing/refund-flow.md"
DOC_BODY = "# Billing — Refund Flow\n\nHow a refund runs.\n"


def _commit(repo, message: str, files: dict[str, str]) -> str:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def _document(repo, *shas: str, docs: dict[str, str] | None = None) -> str:
    """The hook's follow-up commit: a history doc per documented commit, plus the feature doc."""
    files = {f"specs/history/{sha[:8]}.md": f"---\nsha: {sha}\n---\n\n# Commit {sha[:8]}\n" for sha in shas}
    files.update(docs or {DOC: DOC_BODY})
    return _commit(repo, "docs: sync specky docs [skip specky]", files)


@pytest.fixture
def covered(tmp_repo):
    """A repo where `src/app.py` is described by a doc, well enough to be enforced.

    Two documented commits touched that file, which is what `MIN_LINK_COMMITS` asks for — one
    would only prove the two rode in a commit together (see `weakly_covered`).
    """
    for i in range(2):
        sha = _commit(tmp_repo, f"work on the refund path {i}", {"src/app.py": f"def refund{i}(): ...\n"})
        # The doc body has to change, as a regenerated doc's would: git records a commit as
        # touching a file only if its content moved, so an identical rewrite is no link at all.
        _document(tmp_repo, sha, docs={DOC: f"{DOC_BODY}\nRevision {i}.\n"})
    run_index(tmp_repo)
    return tmp_repo


@pytest.fixture
def weakly_covered(tmp_repo):
    """The same link, from a single commit — a coincidence as far as `check` can tell."""
    sha = _commit(tmp_repo, "one commit, many files", {"src/app.py": "x\n", "src/tests.py": "y\n"})
    _document(tmp_repo, sha)
    run_index(tmp_repo)
    return tmp_repo


# --- doc_files, the precomputed coverage map ------------------------------------------------


def _map(repo) -> dict[tuple[str, str], int]:
    conn = db.connect(repo)
    try:
        return {(p, d): n for p, d, n in conn.execute("SELECT path, doc_path, commits FROM doc_files")}
    finally:
        conn.close()


def test_indexing_maps_a_docs_commit_to_the_files_that_commit_touched(covered):
    assert _map(covered) == {("src/app.py", DOC): 2}  # both documented commits paired the two


def test_a_pair_from_one_commit_is_recorded_with_a_count_of_one(weakly_covered):
    assert _map(weakly_covered) == {("src/app.py", DOC): 1, ("src/tests.py", DOC): 1}


def test_the_map_survives_a_fresh_clone(covered, tmp_path):
    """The whole point of deriving this from git: `.specky/` is gitignored, so a CI checkout has
    none of the state the post-commit hook wrote. A map built from `commit_links` would be empty
    there and `specky check` could never fail — the one place it's meant to run."""
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(covered), str(clone)], check=True)
    run_index(clone)

    assert _map(clone) == _map(covered)


def test_a_backfill_commit_documents_the_commits_its_history_docs_name(tmp_repo):
    """`specky sync` writes one doc-sync commit covering many old commits. Its parent is only the
    last of them, so the history docs' shas are what says whose code these docs describe."""
    first = _commit(tmp_repo, "old work", {"src/one.py": "1\n"})
    second = _commit(tmp_repo, "newer work", {"src/two.py": "2\n"})
    _document(tmp_repo, first, second)
    run_index(tmp_repo)

    assert _map(tmp_repo) == {("src/one.py", DOC): 1, ("src/two.py", DOC): 1}


def test_a_commit_carrying_both_code_and_a_doc_describes_itself(tmp_repo):
    """Not everything goes through the hook — an agent following the `document-domain` skill
    commits the code and the doc together, and that's a link too."""
    _commit(tmp_repo, "feature plus its doc", {"src/app.py": "x\n", DOC: DOC_BODY})
    run_index(tmp_repo)

    assert _map(tmp_repo) == {("src/app.py", DOC): 1}


def test_a_history_doc_naming_a_commit_that_no_longer_exists_is_skipped(tmp_repo):
    """A rebase or a dropped branch leaves history docs pointing at shas that aren't in the repo.
    One unresolvable name mustn't take down the whole index run."""
    sha = _commit(tmp_repo, "real work", {"src/app.py": "x\n"})
    _document(tmp_repo, sha, "deadbeefdeadbeef")
    run_index(tmp_repo)

    assert _map(tmp_repo) == {("src/app.py", DOC): 1}


@pytest.mark.parametrize("doc_path", ["specs/history/abc12345.md", "specs/MODULES.md"])
def test_per_commit_and_index_docs_never_cover_code(tmp_repo, doc_path):
    """A history doc is one commit's narrative and MODULES/GLOSSARY are indexes touched by every
    doc-sync commit; treating either as covering code would pair everything with everything."""
    sha = _commit(tmp_repo, "work", {"src/app.py": "x\n"})
    _document(tmp_repo, sha, docs={doc_path: "# Doc\n"})
    run_index(tmp_repo)

    assert _map(tmp_repo) == {}


@pytest.mark.parametrize("path", ["specs/other/thing.md", ".specky/site/index.html"])
def test_speckys_own_outputs_are_never_recorded_as_covered_code(tmp_repo, path):
    """A doc doesn't 'cover' another doc, and a repo that forgot to gitignore `.specky/`
    shouldn't see specky's own rendered site counted as documented code."""
    sha = _commit(tmp_repo, "specky's own output", {path: "x\n"})
    _document(tmp_repo, sha)
    run_index(tmp_repo)

    assert _map(tmp_repo) == {}


def test_a_bulk_commits_file_list_is_dropped_rather_than_claimed_as_coverage(tmp_repo):
    """A vendored-dependency drop or a repo-wide rename names thousands of files. Treating a doc
    linked to such a commit as describing each of them would make `check` demand that one doc for
    edits all over the repo — this is the bound that keeps the table meaningful on a big repo."""
    bulk = {f"vendor/f{i:04d}.py": "x\n" for i in range(indexer.DOC_FILES_MAX_PER_COMMIT + 1)}
    sha = _commit(tmp_repo, "vendor a dependency", bulk)
    _document(tmp_repo, sha)
    run_index(tmp_repo)

    assert _map(tmp_repo) == {}


def test_reindexing_rebuilds_the_map_rather_than_accumulating(covered):
    run_index(covered)
    conn = db.connect(covered)
    assert conn.execute("SELECT COUNT(*) FROM doc_files").fetchone()[0] == 1
    conn.close()


# --- the gate ------------------------------------------------------------------------------


def test_a_change_that_updates_the_doc_is_clean(covered):
    base = git(covered, "rev-parse", "HEAD").strip()
    _commit(
        covered,
        "change the refund path and its doc",
        {"src/app.py": "def refund(x): ...\n", DOC: "# Billing — Refund Flow\n\nRewritten.\n"},
    )
    report = run_check(covered, base=base)

    assert report.violations == ()
    assert report.code_files == ("src/app.py",)
    assert report.touched_docs == (DOC,)


def test_a_change_that_skips_the_doc_is_a_violation(covered):
    base = git(covered, "rev-parse", "HEAD").strip()
    _commit(covered, "change the refund path only", {"src/app.py": "def refund(x): ...\n"})

    assert run_check(covered, base=base).violations == (Violation("src/app.py", DOC),)


def test_a_link_seen_in_only_one_commit_counts_as_coverage_but_fails_nothing(weakly_covered):
    """`commit_links` pairs a doc with *every* file in the commit that produced it, incidental
    ones included. Failing a build on that would mean any file that ever rode along with a doc
    commit is permanently chained to it."""
    base = git(weakly_covered, "rev-parse", "HEAD").strip()
    _commit(weakly_covered, "change the code only", {"src/app.py": "changed\n"})
    report = run_check(weakly_covered, base=base)

    assert report.violations == ()
    assert report.weak_links == 1
    assert report.uncovered == ()  # still counted as covered
    assert "min_link_commits" in "\n".join(check.report_lines(report))


def test_min_link_commits_is_configurable_down_to_one(weakly_covered):
    (weakly_covered / "specky.toml").write_text("[check]\nmin_link_commits = 1\n")
    base = git(weakly_covered, "rev-parse", "HEAD").strip()
    _commit(weakly_covered, "change the code only", {"src/app.py": "changed\n"})

    assert run_check(weakly_covered, base=base).violations == (Violation("src/app.py", DOC),)


def test_documenting_the_change_in_the_same_domain_satisfies_the_gate(covered):
    """A range that adds a new sibling doc has documented its work in the right place, but the new
    doc can't be in `doc_files` yet — nothing has linked it to a commit. Asking for the *exact*
    covering doc here would demand an edit to an unrelated one."""
    base = git(covered, "rev-parse", "HEAD").strip()
    _commit(
        covered,
        "change the code, document it next door",
        {"src/app.py": "def refund(x): ...\n", "specs/billing/chargebacks.md": "# Chargebacks\n"},
    )

    assert run_check(covered, base=base).violations == ()


def test_a_doc_in_another_domain_does_not_satisfy_the_gate(covered):
    base = git(covered, "rev-parse", "HEAD").strip()
    _commit(
        covered,
        "change the code, document something else",
        {"src/app.py": "def refund(x): ...\n", "specs/shipping/labels.md": "# Labels\n"},
    )

    assert run_check(covered, base=base).violations == (Violation("src/app.py", DOC),)


def test_a_file_no_doc_describes_is_a_warning_not_a_violation(covered):
    """Adopting specky on an existing repo means most files have no doc. Failing on those would
    make the gate unadoptable, so they're only counted."""
    base = git(covered, "rev-parse", "HEAD").strip()
    _commit(covered, "add something undocumented", {"src/other.py": "x = 1\n"})
    report = run_check(covered, base=base)

    assert report.violations == ()
    assert report.uncovered == ("src/other.py",)
    assert report.coverage_percent == 0


def test_a_doc_deleted_since_it_was_linked_is_not_demanded(covered):
    (covered / DOC).unlink()
    _commit(covered, "drop the doc", {})
    run_index(covered)
    base = git(covered, "rev-parse", "HEAD").strip()
    _commit(covered, "change the code", {"src/app.py": "def refund(y): ...\n"})

    assert run_check(covered, base=base).violations == ()


def test_a_commit_with_no_history_doc_is_reported_without_failing(covered):
    base = git(covered, "rev-parse", "HEAD").strip()
    sha = _commit(covered, "an undocumented commit", {"README.md": "# changed\n"})
    report = run_check(covered, base=base)

    assert report.undocumented_commits == ((sha, "an undocumented commit"),)
    assert report.violations == ()


def test_specky_s_own_doc_sync_commits_are_not_counted_as_undocumented(covered):
    base = git(covered, "rev-parse", "HEAD").strip()
    _commit(covered, "docs: sync specky docs [skip specky]", {"specs/x/y.md": "# Y\n"})

    assert run_check(covered, base=base).undocumented_commits == ()


def test_the_target_branch_moving_on_after_the_fork_is_not_this_ranges_fault(covered):
    """`git diff base...HEAD` compares against the merge base, so a file the base branch changed
    after this branch forked isn't in this branch's diff — with `..` it would be, and every
    contributor would be asked to update docs for someone else's commit."""
    fork_point = git(covered, "rev-parse", "HEAD").strip()
    git(covered, "checkout", "-q", "-b", "feature")
    _commit(covered, "my change", {"src/mine.py": "mine = 1\n"})
    git(covered, "checkout", "-q", "-")
    _commit(covered, "someone else's change", {"src/app.py": "def refund(z): ...\n"})
    base = git(covered, "rev-parse", "HEAD").strip()
    git(covered, "checkout", "-q", "feature")

    report = run_check(covered, base=base)
    assert report.code_files == ("src/mine.py",)  # not src/app.py, and so no violation
    assert report.violations == ()
    assert fork_point != base


# --- the ignore list -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, ignored",
    [
        (".github/workflows/ci.yml", True),
        ("README.md", True),
        ("uv.lock", True),
        ("package-lock.json", True),
        ("requirements.txt", True),
        ("setup.cfg", True),
        (".specky/index.db", True),
        ("src/specky/db.py", False),
        ("github/app.py", False),  # not the `.github/` directory
    ],
)
def test_the_default_ignore_list(path, ignored):
    assert CheckConfig().ignores(path) is ignored


def test_ignored_files_are_not_counted_as_changed_code(covered):
    base = git(covered, "rev-parse", "HEAD").strip()
    _commit(covered, "ci and readme", {".github/workflows/ci.yml": "on: push\n", "README.md": "# x\n"})

    assert run_check(covered, base=base).code_files == ()


def test_the_ignore_list_is_configurable(tmp_repo):
    (tmp_repo / "specky.toml").write_text('[check]\nignore = ["vendor/"]\n')
    config = CheckConfig.load(tmp_repo)

    assert config.ignores("vendor/lib.py")
    assert not config.ignores("README.md")  # a configured list replaces the defaults


def test_a_single_ignore_string_is_accepted(tmp_repo):
    (tmp_repo / "specky.toml").write_text('[check]\nignore = "vendor/"\n')
    assert CheckConfig.load(tmp_repo).ignore == ("vendor/",)


def test_no_check_table_means_the_defaults(tmp_repo):
    assert CheckConfig.load(tmp_repo) == CheckConfig()


def test_pyproject_can_carry_the_policy_because_specky_toml_is_gitignored(tmp_repo):
    """`specky init` writes a gitignored specky.toml, so a policy kept only there doesn't exist in
    CI — the one place this gate runs."""
    (tmp_repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[tool.specky.check]\nignore = ["vendor/"]\nmin_link_commits = 1\n'
    )
    config = CheckConfig.load(tmp_repo)

    assert config.ignore == ("vendor/",)
    assert config.min_link_commits == 1


def test_specky_toml_wins_over_pyproject(tmp_repo):
    (tmp_repo / "pyproject.toml").write_text("[tool.specky.check]\nmin_link_commits = 1\n")
    (tmp_repo / "specky.toml").write_text("[check]\nmin_link_commits = 5\n")

    assert CheckConfig.load(tmp_repo).min_link_commits == 5


def test_a_pyproject_without_a_specky_table_is_the_defaults(tmp_repo):
    (tmp_repo / "pyproject.toml").write_text('[project]\nname = "x"\n')
    assert CheckConfig.load(tmp_repo) == CheckConfig()


# --- the range -----------------------------------------------------------------------------


def test_the_default_base_is_the_previous_commit(covered):
    assert resolve_base(covered) == "HEAD~1"


def test_the_default_base_prefers_the_branch_a_pull_request_would_target(covered):
    """In CI `origin/HEAD` is the target branch, which is the range a PR should be judged on."""
    git(covered, "update-ref", "refs/remotes/origin/main", "HEAD~1")
    git(covered, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    assert resolve_base(covered) == "origin/HEAD"


def test_a_repo_with_one_commit_compares_against_the_empty_tree(tmp_path):
    repo = tmp_path / "solo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "commit.gpgsign", "false")
    _commit(repo, "first", {"src/app.py": "x = 1\n"})
    run_index(repo)

    assert resolve_base(repo) == check.EMPTY_TREE
    assert run_check(repo).code_files == ("src/app.py",)


def test_a_base_that_isnt_a_revision_is_a_clear_error(covered):
    with pytest.raises(ValueError, match="isn't a revision"):
        run_check(covered, base="v9.9.9-nope")


def test_since_accepts_a_revision(covered):
    sha = git(covered, "rev-parse", "HEAD").strip()
    assert resolve_base(covered, since=sha) == sha


def test_since_accepts_a_date_and_starts_before_the_oldest_commit_in_it(covered):
    """`--since "2 weeks ago"` has to include the oldest commit in that window, so the base is
    that commit's *parent* — using the commit itself would silently drop its own changes."""
    base = resolve_base(covered, since="2 weeks ago")

    assert base.endswith("^") or base == check.EMPTY_TREE
    assert "src/app.py" in run_check(covered, since="2 weeks ago").code_files


def test_a_date_window_with_no_commits_is_an_empty_range(tmp_path):
    """Backdated on purpose: git's approxidate clamps a *future* `--since` to now, so the only
    way to ask for a genuinely empty window is a repo whose commits are all older than it."""
    repo = tmp_path / "old"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    subprocess.run(
        ["git", "commit", "-q", "-m", "old work"],
        cwd=repo,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2020-01-01T00:00:00+0000",
            "GIT_COMMITTER_DATE": "2020-01-01T00:00:00+0000",
        },
    )

    assert resolve_base(repo, since="2021-01-01") == "HEAD"
    assert run_check(repo, since="2021-01-01").code_files == ()


# --- output --------------------------------------------------------------------------------


def _report(**overrides) -> Report:
    fields = {
        "base": "origin/main",
        "code_files": ("src/app.py",),
        "touched_docs": (),
        "violations": (Violation("src/app.py", DOC),),
        "uncovered": (),
        "undocumented_commits": (),
    }
    return Report(**{**fields, **overrides})


def test_the_report_names_the_file_and_the_doc_it_should_have_updated():
    text = "\n".join(check.report_lines(_report()))
    assert f"src/app.py → {DOC}" in text
    assert "1/1 changed files described by a doc (100%)" in text


def test_the_report_tells_a_repo_with_no_hook_how_to_catch_up():
    text = "\n".join(
        check.report_lines(_report(violations=(), undocumented_commits=(("abc12345", "a change"),)))
    )
    assert "install-git-hook" in text and "specky sync" in text
    assert "abc12345 a change" in text


def test_a_range_with_no_code_changes_reports_full_coverage():
    report = _report(code_files=(), violations=())
    assert report.coverage_percent == 100
    assert "0/0" in "\n".join(check.report_lines(report))


# --- the CLI -------------------------------------------------------------------------------


def _run_cli(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["specky", "check", *argv])
    cli.main()


@pytest.fixture
def violating(covered, monkeypatch):
    monkeypatch.chdir(covered)
    base = git(covered, "rev-parse", "HEAD").strip()
    _commit(covered, "change the code only", {"src/app.py": "def refund(x): ...\n"})
    return base


def test_a_violation_exits_1(violating, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exit_info:
        _run_cli(monkeypatch, "--base", violating)

    assert exit_info.value.code == 1
    assert DOC in capsys.readouterr().out


def test_advisory_prints_the_same_report_and_exits_0(violating, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exit_info:
        _run_cli(monkeypatch, "--base", violating)
    failing = capsys.readouterr().out
    assert exit_info.value.code == 1

    _run_cli(monkeypatch, "--base", violating, "--advisory")
    assert capsys.readouterr().out == failing


def test_a_clean_range_exits_0(covered, monkeypatch, capsys):
    monkeypatch.chdir(covered)
    base = git(covered, "rev-parse", "HEAD").strip()
    _commit(covered, "code and doc", {"src/app.py": "def refund(x): ...\n", DOC: "# Doc\n\nNew.\n"})

    _run_cli(monkeypatch, "--base", base)
    assert "Coverage: 1/1" in capsys.readouterr().out


def test_json_output_carries_the_violations_and_the_coverage(violating, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _run_cli(monkeypatch, "--base", violating, "--json")

    payload = json.loads(capsys.readouterr().out)
    assert payload["violations"] == [{"path": "src/app.py", "doc_path": DOC}]
    assert payload["coverage"] == {"covered": 1, "changed": 1, "percent": 100}
    assert payload["base"] == violating


def test_json_output_still_fails_the_build(violating, monkeypatch):
    """A machine-readable report is for annotations, not a way around the gate."""
    with pytest.raises(SystemExit) as exit_info:
        _run_cli(monkeypatch, "--base", violating, "--json")
    assert exit_info.value.code == 1


def test_a_bad_base_is_one_stderr_line_not_a_traceback(covered, monkeypatch, capsys):
    monkeypatch.chdir(covered)
    with pytest.raises(SystemExit) as exit_info:
        _run_cli(monkeypatch, "--base", "v9.9.9-nope")

    assert exit_info.value.code == 1
    assert capsys.readouterr().err.startswith("specky check: ")
