"""`specky tests` — the docs → pytest scaffold path.

The parsing tests feed in doc text directly, because the shapes that matter are the ones a model
actually writes: the per-commit generator's `| Given | When | Then |`, the document-domain skill's
`| Scenario | Given | When | Then (expected) |`, and the malformed tables both produce sometimes.
"""

from __future__ import annotations

import argparse
import ast

import pytest

from specky import cli
from specky.indexer import run_index
from specky.testgen import (
    Scenario,
    Suite,
    collect,
    emit,
    out_path_for,
    parse_scenarios,
    render_suite,
    report_lines,
)

GENERATOR_STYLE = """# Billing — Refund Flow

## What It Does
Refunds money.

## Acceptance Tests

| Given | When | Then |
|-------|------|------|
| A settled payment | The agent refunds it | The customer is credited within a day |
| A payment already refunded | The agent refunds it again | The request is rejected |

## Notes
Not a table.
"""

SKILL_STYLE = """# Billing — Refund Limits

## Acceptance Tests

| Scenario | Given | When | Then (expected) |
|---|---|---|---|
| Over the cap | A £600 refund | The agent submits it | It needs a manager's approval |
"""


# --- parsing --------------------------------------------------------------------------------


def test_the_per_commit_generators_table_shape_is_parsed():
    scenarios = parse_scenarios(GENERATOR_STYLE)
    assert [s.then for s in scenarios] == [
        "The customer is credited within a day",
        "The request is rejected",
    ]
    assert scenarios[0].given == "A settled payment"
    assert scenarios[0].when == "The agent refunds it"
    assert scenarios[0].name == ""


def test_the_skills_four_column_shape_is_parsed_by_header_not_position():
    """`Then (expected)` isn't the third column and isn't spelled `Then` — matching on position
    would have read the *When* column as the assertion."""
    scenario = parse_scenarios(SKILL_STYLE)[0]
    assert scenario.name == "Over the cap"
    assert scenario.given == "A £600 refund"
    assert scenario.then == "It needs a manager's approval"


def test_only_the_acceptance_tests_section_is_read():
    doc = """# Doc

## Outcomes

| Given | When | Then |
|---|---|---|
| Not this table | It is under Outcomes | It must be ignored |

## Acceptance Tests

| Given | When | Then |
|---|---|---|
| This one | It is under the right heading | It is used |
"""
    assert [s.given for s in parse_scenarios(doc)] == ["This one"]


def test_a_doc_with_no_acceptance_tests_section_yields_nothing():
    assert parse_scenarios("# Doc\n\nJust prose.\n") == []


@pytest.mark.parametrize("then", ["", "-", "N/A", "none", "TBD", "_—_"])
def test_a_placeholder_row_is_not_a_scenario(then):
    """Both doc-generation paths are told to keep the section even when there's nothing testable,
    so a placeholder row is the expected shape of that — not something to scaffold."""
    doc = f"## Acceptance Tests\n\n| Given | When | Then |\n|---|---|---|\n| x | y | {then} |\n"
    assert parse_scenarios(doc) == []


def test_a_ragged_row_is_skipped_rather_than_crashing():
    doc = (
        "## Acceptance Tests\n\n| Given | When | Then |\n|---|---|---|\n"
        "| only one cell |\n| a | b | c |\n"
    )
    assert [s.then for s in parse_scenarios(doc)] == ["c"]


def test_a_table_with_unrecognisable_headers_is_left_alone():
    """Better to emit nothing than to guess which of five columns is the assertion."""
    doc = (
        "## Acceptance Tests\n\n| Input | Output | Notes | Owner |\n|---|---|---|---|\n"
        "| a | b | c | d |\n"
    )
    assert parse_scenarios(doc) == []


def test_a_bare_three_column_table_is_read_positionally():
    doc = "## Acceptance Tests\n\n| Setup | Action | Result |\n|---|---|---|\n| a | b | c |\n"
    assert parse_scenarios(doc) == [Scenario(given="a", when="b", then="c")]


def test_an_escaped_pipe_stays_inside_its_cell():
    doc = (
        "## Acceptance Tests\n\n| Given | When | Then |\n|---|---|---|\n"
        r"| a | `specky search 'a \| b'` | it matches | " + "\n"
    )
    assert parse_scenarios(doc)[0].when == "`specky search 'a | b'`"


# --- rendering ------------------------------------------------------------------------------


def _suite(content: str, doc_path: str = "specs/billing/refund-flow.md") -> Suite:
    return Suite(doc_path, out_path_for(doc_path), tuple(parse_scenarios(content)))


def _functions(content: str) -> list[ast.FunctionDef]:
    tree = ast.parse(render_suite(_suite(content)))
    return [n for n in tree.body if isinstance(n, ast.FunctionDef)]


def test_the_output_path_mirrors_the_doc_path():
    assert out_path_for("specs/cli/cost.md") == "tests/spec/test_cli_cost.py"
    assert out_path_for("specs/docs/search-and-indexing.md") == (
        "tests/spec/test_docs_search_and_indexing.py"
    )


def test_a_rendered_suite_is_valid_python():
    """The one thing a scaffold generator must never do: write a file that won't import."""
    ast.parse(render_suite(_suite(GENERATOR_STYLE)))


def test_a_rendered_suite_is_laid_out_the_way_a_linter_expects():
    """These land in someone else's repo, under whatever linter it runs: two blank lines between
    top-level definitions, one trailing newline, nothing else."""
    text = render_suite(_suite(GENERATOR_STYLE))
    assert "\n\n\n\n" not in text
    assert text.endswith('"""\n')
    assert "import pytest\n\n\n@pytest.mark.skip" in text


def test_every_row_becomes_one_skipped_test_naming_its_source_doc():
    text = render_suite(_suite(GENERATOR_STYLE))
    functions = _functions(GENERATOR_STYLE)
    assert len(functions) == 2
    assert all(f.name.startswith("test_") for f in functions)
    assert text.count('@pytest.mark.skip(reason="scaffold from specs/billing/refund-flow.md")') == 2
    # The Then is the assertion the reader has to write, so it has to survive into the file.
    assert "The customer is credited within a day" in ast.get_docstring(functions[0])


def test_a_scenario_column_names_the_test():
    assert _functions(SKILL_STYLE)[0].name == "test_over_the_cap"


def test_two_identical_scenarios_get_distinct_function_names():
    doc = (
        "## Acceptance Tests\n\n| Given | When | Then |\n|---|---|---|\n"
        "| same | same | first |\n| same | same | second |\n"
    )
    assert len({f.name for f in _functions(doc)}) == 2


def test_a_scenario_that_would_end_the_docstring_early_is_neutralised():
    doc = (
        '## Acceptance Tests\n\n| Given | When | Then |\n|---|---|---|\n'
        '| a """ quote | a trailing backslash \\ | it still parses |\n'
    )
    text = render_suite(_suite(doc))
    ast.parse(text)  # would raise a SyntaxError if either got through verbatim
    assert "it still parses" in text


def test_a_row_with_no_letters_or_digits_still_gets_a_usable_name():
    doc = "## Acceptance Tests\n\n| Given | When | Then |\n|---|---|---|\n| — | — | ok |\n"
    assert _functions(doc)[0].name == "test_scenario"


# --- collecting and writing -----------------------------------------------------------------


def test_history_docs_are_never_scaffolded(tmp_repo, write_doc):
    """One doc per commit: on a repo with thousands of commits this is where all the work would
    go, and none of it would be a scaffold anyone wants."""
    write_doc("history/abc12345.md", GENERATOR_STYLE)
    write_doc("billing/refund-flow.md", GENERATOR_STYLE, {"type": "workflow"})
    run_index(tmp_repo)
    assert [s.doc_path for s in collect(tmp_repo)] == ["specs/billing/refund-flow.md"]


def test_an_unclassified_hand_written_doc_is_still_scaffolded(tmp_repo, write_doc):
    """Coverage here is "has a table", not "has frontmatter" — a doc written by hand or by the
    skill before tagging still describes behaviour worth pinning down."""
    write_doc("billing/refund-flow.md", GENERATOR_STYLE)
    run_index(tmp_repo)
    assert len(collect(tmp_repo)) == 1


def test_emit_writes_one_file_per_doc_and_reports_it(tmp_repo, write_doc):
    write_doc("billing/refund-flow.md", GENERATOR_STYLE, {"type": "workflow"})
    write_doc("billing/refund-limits.md", SKILL_STYLE, {"type": "feature"})
    run_index(tmp_repo)

    result = emit(tmp_repo)
    assert result.written == (
        "tests/spec/test_billing_refund_flow.py",
        "tests/spec/test_billing_refund_limits.py",
    )
    assert result.scenarios == 3
    assert (tmp_repo / "tests/spec/test_billing_refund_flow.py").exists()
    assert "3 scenario(s)" in "\n".join(report_lines(result))


def test_a_second_run_leaves_a_filled_in_scaffold_alone(tmp_repo, write_doc):
    """The scaffold is the starting point for work someone then does by hand. Overwriting it by
    default would throw that work away on the next `specky tests`."""
    write_doc("billing/refund-flow.md", GENERATOR_STYLE, {"type": "workflow"})
    run_index(tmp_repo)
    emit(tmp_repo)
    target = tmp_repo / "tests/spec/test_billing_refund_flow.py"
    target.write_text("# my own work\n")

    result = emit(tmp_repo)
    assert result.written == ()
    assert result.skipped == ("tests/spec/test_billing_refund_flow.py",)
    assert target.read_text() == "# my own work\n"
    assert "--force" in "\n".join(report_lines(result))

    assert emit(tmp_repo, force=True).written == ("tests/spec/test_billing_refund_flow.py",)
    assert target.read_text() != "# my own work\n"


def test_nothing_to_scaffold_says_so(tmp_repo, write_doc):
    write_doc("billing/refund-flow.md", "# Refunds\n\nNo table here.\n")
    run_index(tmp_repo)
    result = emit(tmp_repo)
    assert result.written == ()
    assert "no `## Acceptance Tests` tables" in "\n".join(report_lines(result))
    assert not (tmp_repo / "tests/spec").exists()  # no empty directory left behind


def test_the_scaffolds_pytest_collects_and_skips(tmp_repo, write_doc):
    """The end of the loop: what specky writes has to be a file pytest can actually run."""
    write_doc("billing/refund-flow.md", GENERATOR_STYLE, {"type": "workflow"})
    run_index(tmp_repo)
    emit(tmp_repo)

    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/spec", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout
    assert "2 skipped" in result.stdout


def test_the_cli_wires_force_through(tmp_repo, write_doc, monkeypatch, capsys):
    write_doc("billing/refund-flow.md", GENERATOR_STYLE, {"type": "workflow"})
    run_index(tmp_repo)
    monkeypatch.chdir(tmp_repo)

    cli._tests(argparse.Namespace(force=False, emit="pytest"))
    assert "wrote tests/spec/test_billing_refund_flow.py" in capsys.readouterr().out
    cli._tests(argparse.Namespace(force=False, emit="pytest"))
    assert "pass --force" in capsys.readouterr().out
    cli._tests(argparse.Namespace(force=True, emit="pytest"))
    assert "wrote tests/spec/test_billing_refund_flow.py" in capsys.readouterr().out
