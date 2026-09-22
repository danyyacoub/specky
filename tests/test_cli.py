import argparse

import pytest

from specky import cli


def _run(argv: list[str]) -> None:
    """Invoke main() as the console script would."""
    import sys

    sys.argv = ["specky", *argv]
    cli.main()


def _subcommands() -> dict[str, argparse.ArgumentParser]:
    action = next(
        a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction)
    )
    return action.choices


def test_every_subcommand_is_wired_to_a_handler():
    """The dispatch is `args.func(args)`, so a subparser without a `func` default would
    crash with AttributeError instead of running anything."""
    assert _subcommands()
    for name, sub in _subcommands().items():
        assert callable(sub.get_default("func")), name


def test_there_is_no_dead_or_duplicate_subcommand():
    names = set(_subcommands())
    assert "generate" not in names  # was a stub that only printed "not implemented yet"
    assert "mcp" not in names  # the `specky-mcp` console script is the one entry point


def test_sync_flags_reach_the_handler(monkeypatch):
    """`specky sync` on a real repo is hundreds of billable calls, so the narrowing flags have
    to be wired all the way through, not just declared."""
    calls = {}
    monkeypatch.setattr(cli, "_sync", lambda args: calls.update(vars(args)))
    _run(["sync", "--since", "HEAD~5", "--limit", "3", "--dry-run", "--yes", "--all-branches"])
    assert (calls["since"], calls["limit"], calls["dry_run"], calls["yes"], calls["all_branches"]) == (
        "HEAD~5",
        3,
        True,
        True,
        True,
    )

    defaults = _subcommands()["sync"].parse_args([])
    assert (
        defaults.since,
        defaults.limit,
        defaults.dry_run,
        defaults.yes,
        defaults.all_branches,
    ) == (None, None, False, False, False)


def test_index_reports_what_it_indexed(tmp_repo, write_doc, monkeypatch, capsys):
    write_doc("billing/refund-flow.md", "# Refunds\n")
    monkeypatch.chdir(tmp_repo)
    _run(["index"])
    out = capsys.readouterr().out
    assert "indexed 1 docs, 1 commits" in out


def test_cost_prints_a_report_and_clears_the_cache(tmp_repo, monkeypatch, capsys):
    from specky.ai_provider import CachingProvider

    class Fake:
        def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
            return "an answer"

    CachingProvider(Fake(), tmp_repo, model="test-model", command="sync").generate("why?")
    monkeypatch.chdir(tmp_repo)

    _run(["cost"])
    assert "test-model" in capsys.readouterr().out

    _run(["cost", "--clear-cache"])
    assert "cleared 1 cached response(s)" in capsys.readouterr().out


def test_search_without_a_query_is_a_clean_error(tmp_repo, monkeypatch, capsys):
    monkeypatch.chdir(tmp_repo)
    with pytest.raises(SystemExit) as exit_info:
        _run(["search"])
    assert exit_info.value.code == 1
    assert "specky search: provide a query" in capsys.readouterr().err


def test_search_prints_hits(tmp_repo, write_doc, monkeypatch, capsys):
    write_doc("billing/refund-flow.md", "# Refunds\n\nIssue a refund.\n")
    monkeypatch.chdir(tmp_repo)
    _run(["index"])
    _run(["search", "refund"])
    assert "specs/billing/refund-flow.md" in capsys.readouterr().out


def test_a_handler_failure_becomes_one_stderr_line(monkeypatch, capsys):
    """No traceback for a bad config or an unreachable provider — the wrapper in main()
    is what guarantees that for every command, not per-command try/except."""

    def boom(_args):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(cli, "_graph", boom)
    with pytest.raises(SystemExit) as exit_info:
        _run(["graph"])
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "specky graph: provider unreachable"
    assert "Traceback" not in captured.err


def test_an_unknown_command_still_gets_argparse_usage(capsys):
    with pytest.raises(SystemExit) as exit_info:
        _run(["nope"])
    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_version_flag_prints_the_package_version(capsys):
    from specky import __version__

    with pytest.raises(SystemExit) as exit_info:
        _run(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"specky {__version__}"
