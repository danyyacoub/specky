"""`specky doctor` — the checks a user runs when nothing is working.

Two properties matter more than the individual messages, and both are asserted below:

- A fresh repo that has simply never been set up exits 0. If it didn't, `specky doctor` couldn't
  go into anyone's CI, and "not configured yet" would look like "broken".
- No secret is ever printed. The API-key check reports set/not-set only.
"""

import json
import sys

import pytest

from specky import cli, doctor
from specky.commit_doc import POST_COMMIT_HOOK

from conftest import git


@pytest.fixture(autouse=True)
def in_repo(tmp_repo, monkeypatch):
    """run_checks() resolves the repo from the process cwd, like the real CLI does."""
    monkeypatch.chdir(tmp_repo)
    return tmp_repo


def _by_section(checks: list[doctor.Check]) -> dict[str, list[doctor.Check]]:
    grouped: dict[str, list[doctor.Check]] = {}
    for check in checks:
        grouped.setdefault(check.section, []).append(check)
    return grouped


def _statuses(checks: list[doctor.Check], section: str) -> list[str]:
    return [c.status for c in checks if c.section == section]


def _write_config(repo, body: str) -> None:
    (repo / "specky.toml").write_text(body)


def _install_hook(repo) -> None:
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(POST_COMMIT_HOOK)
    hook.chmod(0o755)


# --- the fresh-repo contract -------------------------------------------------------------


def test_an_unconfigured_repo_only_warns(in_repo):
    """No config, no hook, no index, no site — every one of those is fixed by a command specky
    tells you to run, so none of them is a failure."""
    checks = doctor.run_checks()

    assert doctor.worst(checks) == doctor.WARN
    for section in ("config", "git hook", "index", "site"):
        assert _statuses(checks, section) == [doctor.WARN], section
    assert "specky init" in " ".join(c.detail for c in checks if c.section == "config")


def test_the_repo_root_is_reported(in_repo):
    root = _by_section(doctor.run_checks())["repo"][0]
    assert root.status == doctor.OK and root.detail == str(in_repo)


def test_outside_a_git_repo_it_fails_and_stops(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # tmp_path itself is not a repo
    checks = doctor.run_checks()

    assert doctor.worst(checks) == doctor.FAIL
    assert _statuses(checks, "repo") == [doctor.FAIL]
    # The repo-dependent checks are skipped rather than reported wrongly.
    assert "config" not in _by_section(checks)


# --- config ------------------------------------------------------------------------------


def test_a_valid_command_provider_passes(in_repo):
    _write_config(in_repo, '[ai]\nprovider = "command"\ncommand = "git --version"\n')
    assert _statuses(doctor.run_checks(), "config") == [doctor.OK, doctor.OK]


def test_a_provider_command_that_isnt_installed_fails(in_repo):
    _write_config(in_repo, '[ai]\nprovider = "command"\ncommand = "definitely-not-a-real-binary -p"\n')
    checks = _by_section(doctor.run_checks())["config"]

    assert [c.status for c in checks] == [doctor.OK, doctor.FAIL]
    assert "definitely-not-a-real-binary" in checks[-1].detail


def test_broken_toml_fails_without_a_traceback(in_repo):
    _write_config(in_repo, "[ai\nprovider =\n")
    checks = _by_section(doctor.run_checks())["config"]

    assert [c.status for c in checks] == [doctor.FAIL]
    assert "not valid TOML" in checks[0].detail


def test_an_unusable_provider_config_fails(in_repo):
    """The same ConfigError the hook would hit on the next commit, surfaced before it happens."""
    _write_config(in_repo, '[ai]\nprovider = "openai-compatible"\nmodel = "x"\n')
    checks = _by_section(doctor.run_checks())["config"]

    assert [c.status for c in checks] == [doctor.FAIL]
    assert "base_url" in checks[0].detail


def test_a_missing_api_key_fails_and_a_present_one_is_never_printed(in_repo, monkeypatch):
    _write_config(
        in_repo,
        '[ai]\nprovider = "anthropic"\nmodel = "claude-haiku-4-5"\napi_key_env = "SPECKY_TEST_KEY"\n',
    )
    monkeypatch.delenv("SPECKY_TEST_KEY", raising=False)
    checks = _by_section(doctor.run_checks())["config"]
    assert [c.status for c in checks] == [doctor.OK, doctor.FAIL]
    assert "SPECKY_TEST_KEY is not set" in checks[-1].detail

    monkeypatch.setenv("SPECKY_TEST_KEY", "sk-do-not-print-me")
    checks = _by_section(doctor.run_checks())["config"]
    assert [c.status for c in checks] == [doctor.OK, doctor.OK]
    assert "sk-do-not-print-me" not in doctor.report(doctor.run_checks())


# --- git hook ----------------------------------------------------------------------------


def test_speckys_own_hook_passes(in_repo):
    _install_hook(in_repo)
    assert _statuses(doctor.run_checks(), "git hook") == [doctor.OK]


def test_someone_elses_post_commit_hook_is_a_failure(in_repo):
    """`install-git-hook` refuses to overwrite a foreign hook, so this state doesn't fix itself —
    it needs a human to merge the two, which is the one thing a warn wouldn't say."""
    hook = in_repo / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nmake lint\n")
    hook.chmod(0o755)

    checks = _by_section(doctor.run_checks())["git hook"]
    assert [c.status for c in checks] == [doctor.FAIL]
    assert "wasn't installed by specky" in checks[0].detail


def test_a_non_executable_hook_is_a_failure(in_repo):
    """git skips a non-executable hook without a word, which looks exactly like specky being
    broken."""
    _install_hook(in_repo)
    (in_repo / ".git" / "hooks" / "post-commit").chmod(0o644)

    checks = _by_section(doctor.run_checks())["git hook"]
    assert [c.status for c in checks] == [doctor.FAIL]
    assert "not executable" in checks[0].detail


# --- index, site, backlog ----------------------------------------------------------------


def test_an_indexed_repo_reports_its_counts(in_repo, write_doc):
    from specky.indexer import run_index

    write_doc("billing/refund-flow.md", "# Refunds\n\nIssue a refund.\n")
    run_index(in_repo)

    checks = _by_section(doctor.run_checks())["index"]
    assert [c.status for c in checks] == [doctor.OK]  # no wal warning
    assert "1 docs, 1 commits indexed" in checks[0].detail


def test_an_index_missing_its_tables_fails(in_repo):
    db_path = in_repo / ".specky" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    db_path.write_bytes(b"")  # an empty file is a valid, tableless sqlite db

    checks = _by_section(doctor.run_checks())["index"]
    assert [c.status for c in checks] == [doctor.FAIL]
    assert "specky index" in checks[0].detail


def test_a_rendered_site_reports_its_page_count(in_repo, write_doc):
    from specky.html_render import render_site
    from specky.indexer import run_index

    write_doc("billing/refund-flow.md", "# Refunds\n\nIssue a refund.\n")
    run_index(in_repo)
    render_site(in_repo)

    checks = _by_section(doctor.run_checks())["site"]
    assert [c.status for c in checks] == [doctor.OK]
    assert "2 pages" in checks[0].detail  # the doc plus index.html


def test_undocumented_recent_commits_are_reported(in_repo):
    checks = _by_section(doctor.run_checks())["docs"]
    assert [c.status for c in checks] == [doctor.WARN]
    assert "1 of the last 1 documentable commits" in checks[0].detail
    assert "specky sync --dry-run" in checks[0].detail


def test_a_documented_history_passes(in_repo):
    from specky import commit_doc

    sha = git(in_repo, "rev-parse", "HEAD").strip()
    commit_doc.write_history_file(
        in_repo,
        commit_doc.Commit(sha=sha, author="a", date="2026-01-01", message="initial commit", diff=""),
        "summary",
    )
    assert _statuses(doctor.run_checks(), "docs") == [doctor.OK]


def test_speckys_own_doc_sync_commits_dont_count_as_a_backlog(in_repo):
    """They're never documented by design, so counting them would mean `doctor` warns forever on
    any repo that's actually working."""
    from specky import commit_doc

    (in_repo / "specs" / "note.md").write_text("hi\n")
    git(in_repo, "add", "-A")
    git(in_repo, "commit", "-q", "-m", commit_doc._AUTO_COMMIT_MARKER)

    checks = _by_section(doctor.run_checks())["docs"]
    assert "of the last 1 documentable commits" in checks[0].detail  # 2 commits, 1 considered


def test_the_backlog_probe_is_bounded(in_repo, monkeypatch):
    """specky runs on other people's repos, which have more than 40 commits. The probe answers
    "is the hook working now", so it must not walk history to do it."""
    walked = {}
    real_run = doctor._run

    def spy(args, cwd=None):
        if args[:2] == ["git", "log"]:
            walked["args"] = args
        return real_run(args, cwd=cwd)

    monkeypatch.setattr(doctor, "_run", spy)
    doctor.run_checks()
    assert f"-{doctor.BACKLOG_PROBE_COMMITS}" in walked["args"]
    assert doctor.BACKLOG_PROBE_COMMITS <= 100  # a window, not a history walk


# --- the command itself ------------------------------------------------------------------


def _run_cli(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["specky", "doctor", *argv])
    cli.main()


def test_the_command_exits_zero_on_warnings(in_repo, monkeypatch, capsys):
    _run_cli(monkeypatch)  # no SystemExit — a fresh repo is not a CI failure
    assert "[warn]" in capsys.readouterr().out


def test_the_command_exits_one_on_a_failure(in_repo, monkeypatch, capsys):
    _write_config(in_repo, "[ai\n")
    with pytest.raises(SystemExit) as exit_info:
        _run_cli(monkeypatch)
    assert exit_info.value.code == 1
    assert "[fail]" in capsys.readouterr().out


def test_json_output_is_machine_readable(in_repo, monkeypatch, capsys):
    _run_cli(monkeypatch, "--json")
    rows = json.loads(capsys.readouterr().out)

    assert {"section", "status", "detail"} == set(rows[0])
    assert {r["status"] for r in rows} <= {doctor.OK, doctor.WARN, doctor.FAIL}
    assert any(r["section"] == "repo" and r["detail"] == str(in_repo) for r in rows)


def test_the_report_is_grouped_by_section(in_repo):
    report = doctor.report(doctor.run_checks())
    assert report.startswith("== toolchain ==")
    assert "== git hook ==" in report
    assert all(line.startswith(("==", "  [")) for line in report.splitlines())
