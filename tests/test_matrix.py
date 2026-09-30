"""```matrix blocks — parse, check, render, and the testgen/lint/doc_tools paths that read them."""

from __future__ import annotations

import json
import sqlite3

import pytest

from specky import matrix, testgen
from specky.matrix import MatrixError, evaluate_node, parse, parse_expr

BLOCK = """
name: price_variance
inputs:
  ordered: number
  delivered: number
  billed_qty: number
  billed_price: number
  ref_price: number
expect:
  price_var: number
  qty_var: number
  verdict: enum[conform, to_control]
formulas:
  price_var = (billed_price - ref_price) * billed_qty
  qty_var = max(0, billed_qty - coalesce(delivered, ordered)) * ref_price
  verdict = if(price_var + qty_var > 0, "to_control", "conform")
---
| Over-billing | 100 | 95 | 98 | 33 | 32 | 98 | 96 | to_control |
| No delivery | 100 | | 110 | 32 | 32 | 0 | 320 | |
"""


def _fence(body: str) -> str:
    return f"```matrix\n{body.strip()}\n```\n"


def _eval(source: str, env: dict | None = None):
    return evaluate_node(parse_expr(source), env or {})


# --- grammar / evaluator -------------------------------------------------------


def test_arithmetic_and_precedence():
    assert _eval("1 + 2 * 3") == 7
    assert _eval("(1 + 2) * 3") == 9
    assert _eval("10 / 4") == 2.5
    assert _eval("-4 + abs(-2)") == -2
    assert _eval("-7 % 3") == 2
    assert _eval(".5 + 1e2") == 100.5


def test_comparison_logic_and_literals():
    assert _eval("3 > 2 and 1 <= 1") is True
    assert _eval("not (1 == 2)") is True
    assert _eval('"a" == "a" or 0') is True
    assert _eval("x == null", {"x": None}) is True
    assert _eval("true != false") is True


def test_functions():
    assert _eval("min(3, 1, 2)") == 1
    assert _eval("max(0, 5 - 8)") == 0
    assert _eval("round(1.25, 1)") == 1.3  # half away from zero, not banker's
    assert _eval("round(-2.5)") == -3
    assert _eval('if(1 > 0, "yes", "no")') == "yes"
    assert _eval("coalesce(x, 7)", {"x": None}) == 7


def test_if_only_evaluates_the_branch_it_takes():
    assert _eval("if(x == null, 0, x * 2)", {"x": None}) == 0


@pytest.mark.parametrize(
    "source, env",
    [
        ("nope + 1", {}),
        ('"a" + 1', {}),
        ("5 / 0", {}),
        ("x * 2", {"x": None}),  # a missing value is an error, not a zero
        ('"a" < 3', {}),  # ordering unlike types
        ("min()", {}),
        ("abs(1, 2)", {}),
    ],
)
def test_evaluation_errors_are_matrix_errors(source, env):
    with pytest.raises(MatrixError):
        _eval(source, env)


@pytest.mark.parametrize(
    "source", ["pow(2, 3)", "1 < 2 < 3", "1 +", "(1", "a and", "1 $ 2"]
)
def test_parse_errors(source):
    with pytest.raises(MatrixError):
        parse_expr(source)


def test_matches_is_type_strict_for_bools():
    assert matrix.matches(98.0, 98.00000000001)
    assert not matrix.matches(1.0, True)
    assert matrix.matches(None, None)


# --- block parsing -------------------------------------------------------------


def test_parse_columns_and_rows():
    m = parse(BLOCK)
    assert m.name == "price_variance"
    assert [c.name for c in m.inputs] == [
        "ordered",
        "delivered",
        "billed_qty",
        "billed_price",
        "ref_price",
    ]
    assert [c.name for c in m.expect] == ["price_var", "qty_var", "verdict"]
    assert m.expect[-1].options == ("conform", "to_control")
    assert [r.label for r in m.rows] == ["Over-billing", "No delivery"]
    assert m.rows[1].inputs == (100.0, None, 110.0, 32.0, 32.0)  # empty input → missing
    # An empty expected cell is not asserted.
    assert dict(m.rows[1].expected) == {"price_var": 0.0, "qty_var": 320.0}


def test_cells_coerce_bools_nulls_and_separators():
    m = parse(
        "inputs:\n  amount: number\n  flag: bool\nexpect:\n  out: text\n"
        "---\n| R | 1,234,567.5 | yes | null |"
    )
    assert m.rows[0].inputs == (1234567.5, True)
    assert m.rows[0].expected == (("out", None),)


def test_stated_values_that_agree_with_the_formulas_check_clean():
    m = parse(BLOCK)
    assert [matrix.check_row(m, row) for row in m.rows] == [{}, {}]


def test_check_row_reports_a_disputed_value():
    m = parse(BLOCK.replace("| 98 | 96 | to_control |", "| 95 | 96 | to_control |"))
    assert matrix.check_row(m, m.rows[0]) == {"price_var": 98.0}


@pytest.mark.parametrize(
    "source",
    [
        "inputs:\n  x: bogus\nexpect:\n  y: number",  # unknown type
        "inputs:\n  x: number\nexpect:\n  y: number\n---\n| R | 1 |",  # ragged row
        "inputs:\n  s: enum[a]\nexpect:\n  y: number\n---\n| R | zzz | 1 |",  # outside enum
        "inputs:\n  x: number\n  x: number\nexpect:\n  y: number",  # duplicate column
        "inputs:\n  x: number\nformulas:\n  y = x * 2\n---\n| R | 4 |",  # nothing stated
        "expect:\n  y: number",  # no inputs
        "inputs:\n  x: number\nexpect:\n  y: number\nformulas:\n  x = 1",  # redefines an input
        "name: not-an-identifier\ninputs:\n  x: number\nexpect:\n  y: number",
        "inputs:\n  x: number\nexpect:\n  y: number\n---\n| R | abc | 1 |",  # not a number
    ],
)
def test_bad_blocks_raise(source):
    with pytest.raises(MatrixError):
        parse(source)


def test_parse_blocks_skips_bad_ones_and_block_keys_number_the_unnamed():
    unnamed = "inputs:\n  x: number\nexpect:\n  y: number\n---\n| R | 1 | 2 |"
    doc = (
        _fence(unnamed)
        + _fence("inputs:\n  x: bogus")
        + _fence(BLOCK)
        + _fence(unnamed)
    )
    blocks = matrix.parse_blocks(doc)
    assert len(blocks) == 3
    assert matrix.block_keys(blocks) == ["matrix", "price_variance", "matrix_2"]


def test_fence_must_open_a_line():
    assert matrix.block_sources("A ` ```matrix ` block in prose.\n") == []


# --- rendering -----------------------------------------------------------------


def _code_block(body: str) -> str:
    return f'<pre><code class="language-matrix">{body}</code></pre>'


def test_render_figure_is_a_static_table():
    figure = matrix.render_figure(parse(BLOCK))
    assert figure.startswith('<figure class="tw matrix">')
    assert (
        "<input" not in figure and "<select" not in figure and "<script" not in figure
    )
    assert ">Inputs</th>" in figure and ">Expected</th>" in figure
    assert "<figcaption><code>price_var = " in figure
    assert "mx-bad" not in figure
    assert ">—</td>" in figure  # the missing delivered value


def test_render_marks_a_disputed_value():
    m = parse(BLOCK.replace("| 98 | 96 | to_control |", "| 95 | 96 | to_control |"))
    figure = matrix.render_figure(m)
    assert 'class="mx-exp mx-first mx-bad" title="formula gives 98">95</td>' in figure


def test_large_numbers_render_without_exponent():
    assert matrix.fmt(1234567.89) == "1234567.89"
    assert matrix.fmt(98.0) == "98"
    assert matrix.fmt(0.1 + 0.2) == "0.3"
    assert matrix.fmt(-0.0) == "0"
    assert matrix.fmt(1e16) == "10000000000000000"
    assert matrix.fmt(True) == "yes"


def test_a_formula_error_still_renders_the_stated_values():
    body = "inputs:\n  x: number\nexpect:\n  y: number\nformulas:\n  y = missing + 1\n---\n| R | 1 | 2 |"
    out, _, rendered = matrix.render_matrix_blocks(_code_block(body))
    assert rendered and '<figure class="tw matrix">' in out and ">2</td>" in out


def test_render_matrix_blocks_replaces_fence_and_skips_bad_ones():
    good = "inputs:\n  x: number\nexpect:\n  y: number\n---\n| R | 2 | 3 |"
    out, any_source, any_rendered = matrix.render_matrix_blocks(
        _code_block(good) + _code_block("inputs:\n  x: bogus\n")
    )
    assert any_source and any_rendered
    assert out.count('<figure class="tw matrix">') == 1
    assert "language-matrix" in out  # the broken block kept its source


def test_render_unescapes_the_code_block():
    body = "inputs:\n  s: text\nexpect:\n  y: bool\nformulas:\n  y = s == &quot;a &amp; b&quot;\n---\n| R | a &amp; b | true |"
    out, _, rendered = matrix.render_matrix_blocks(_code_block(body))
    assert rendered and "mx-bad" not in out


def test_limit_leaves_extra_blocks_as_source():
    good = _code_block("inputs:\n  x: number\nexpect:\n  y: number\n---\n| R | 1 | 1 |")
    out, _, rendered = matrix.render_matrix_blocks(good * 2, limit=1)
    assert rendered
    assert out.count('<figure class="tw matrix">') == 1
    assert out.count("language-matrix") == 1


def test_doc_body_renders_the_fence_through_markdown():
    from specky.html_render import render_doc_body

    out, _, _ = render_doc_body(
        "# T\n\n" + _fence(BLOCK), {"billed_qty": "Billed quantity"}
    )
    assert '<figure class="tw matrix">' in out
    assert "language-matrix" not in out


def test_viewer_ships_no_matrix_script():
    from specky import html_render

    assert not any("mxEval" in block for block in html_render.APP_JS_BLOCKS)


# --- testgen -------------------------------------------------------------------


def _doc(*blocks: str) -> str:
    return "# Doc\n\n## Acceptance Tests\n\n" + "".join(_fence(b) for b in blocks)


def test_only_blocks_under_acceptance_tests_with_rows_become_tests():
    content = (
        "# Doc\n\n## Example\n\n"
        + _fence(BLOCK)
        + "\n## Acceptance Tests\n\n"
        + _fence("inputs:\n  x: number\nexpect:\n  y: number")  # no rows
        + _fence(BLOCK)
    )
    [(key, m)] = testgen.parse_matrices(content)
    assert key == "price_variance" and len(m.rows) == 2


def test_matrix_data_holds_stated_values_only():
    suite = testgen.Suite(
        "specs/a/b.md",
        "tests/spec/test_a_b.py",
        (),
        tuple(testgen.parse_matrices(_doc(BLOCK))),
    )
    assert suite.data_path == "tests/spec/test_a_b.matrix.json"
    data = json.loads(testgen.matrix_data(suite))
    rows = data["blocks"]["price_variance"]["rows"]
    assert data["doc"] == "specs/a/b.md"
    assert rows[1]["inputs"]["delivered"] is None
    assert rows[1]["expected"] == {
        "price_var": 0.0,
        "qty_var": 320.0,
    }  # verdict not asserted


def test_render_suite_reads_rows_from_the_data_file(tmp_path):
    suite = testgen.Suite(
        "specs/a/b.md",
        "tests/spec/test_a_b.py",
        (),
        tuple(testgen.parse_matrices(_doc(BLOCK))),
    )
    source = testgen.render_suite(suite)
    compile(source, "test_a_b.py", "exec")
    assert 'with_name("test_a_b.matrix.json")' in source
    assert "def evaluate_price_variance(inputs: dict) -> dict:" in source
    assert 'MATRIX["price_variance"]["rows"]' in source
    assert "98" not in source  # no stamped values: they live in the JSON

    # Wired to a correct evaluator, the generated test passes on every row.
    (tmp_path / "test_a_b.matrix.json").write_text(testgen.matrix_data(suite))
    namespace = {"__file__": str(tmp_path / "test_a_b.py")}
    exec(source, namespace)

    def wired(inputs):
        delivered = (
            inputs["delivered"]
            if inputs["delivered"] is not None
            else inputs["ordered"]
        )
        price = (inputs["billed_price"] - inputs["ref_price"]) * inputs["billed_qty"]
        qty = max(0, inputs["billed_qty"] - delivered) * inputs["ref_price"]
        return {
            "price_var": price,
            "qty_var": qty,
            "verdict": "to_control" if price + qty else "conform",
        }

    for row in namespace["MATRIX"]["price_variance"]["rows"]:
        namespace["_assert_row"](row, wired(row["inputs"]))
    with pytest.raises(AssertionError):
        namespace["_assert_row"](
            namespace["MATRIX"]["price_variance"]["rows"][0],
            {
                **wired(namespace["MATRIX"]["price_variance"]["rows"][0]["inputs"]),
                "qty_var": 95.0,
            },
        )


def _index(repo, docs: dict[str, str]) -> None:
    """A minimal index: testgen reads only `documents(path, content)`."""
    (repo / ".specky").mkdir(exist_ok=True)
    conn = sqlite3.connect(repo / ".specky" / "index.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS documents (path TEXT PRIMARY KEY, content TEXT)"
    )
    conn.execute("DELETE FROM documents")
    conn.executemany("INSERT INTO documents VALUES (?, ?)", docs.items())
    conn.commit()
    conn.close()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(
        testgen, "connect", lambda root: sqlite3.connect(root / ".specky" / "index.db")
    )
    return tmp_path


def test_emit_refreshes_data_without_touching_the_wired_scaffold(repo):
    _index(repo, {"specs/a/b.md": _doc(BLOCK)})
    first = testgen.emit(repo)
    assert first.written == ("tests/spec/test_a_b.py",)
    assert first.refreshed == ("tests/spec/test_a_b.matrix.json",)
    assert first.counts["tests/spec/test_a_b.py"] == 2
    scaffold = repo / "tests/spec/test_a_b.py"
    scaffold.write_text(scaffold.read_text() + "\n# wired by hand\n")

    assert testgen.stale_matrix_files(repo) == []
    assert testgen.emit(repo).refreshed == ()  # nothing changed, nothing rewritten

    _index(
        repo, {"specs/a/b.md": _doc(BLOCK.replace("| 0 | 320 | |", "| 0 | 330 | |"))}
    )
    assert testgen.stale_matrix_files(repo) == ["tests/spec/test_a_b.matrix.json"]
    again = testgen.emit(repo)
    assert again.skipped == ("tests/spec/test_a_b.py",)
    assert again.refreshed == ("tests/spec/test_a_b.matrix.json",)
    assert scaffold.read_text().endswith("# wired by hand\n")
    data = json.loads((repo / "tests/spec/test_a_b.matrix.json").read_text())
    assert data["blocks"]["price_variance"]["rows"][1]["expected"]["qty_var"] == 330.0


def test_emit_empties_data_whose_doc_dropped_its_matrix(repo):
    _index(repo, {"specs/a/b.md": _doc(BLOCK)})
    testgen.emit(repo)
    _index(
        repo,
        {
            "specs/a/b.md": "# Doc\n\n## Acceptance Tests\n\n| Given | When | Then |\n|---|---|---|\n| a | b | c |\n"
        },
    )
    assert testgen.stale_matrix_files(repo) == ["tests/spec/test_a_b.matrix.json"]
    testgen.emit(repo)
    data = json.loads((repo / "tests/spec/test_a_b.matrix.json").read_text())
    assert data == {"doc": "specs/a/b.md", "blocks": {}}


def test_tests_check_exits_nonzero_when_stale(repo, monkeypatch, capsys):
    from specky import cli
    import specky.db

    _index(repo, {"specs/a/b.md": _doc(BLOCK)})
    monkeypatch.setattr(specky.db, "repo_root", lambda: repo)
    with pytest.raises(SystemExit) as exc:
        args = cli.build_parser().parse_args(["tests", "--check"])
        args.func(args)
    assert exc.value.code == 1
    assert "test_a_b.matrix.json is behind the docs" in capsys.readouterr().out
    assert not (repo / "tests/spec").exists()  # --check writes nothing


# --- lint ------------------------------------------------------------------------


def test_lint_flags_bad_unevaluable_and_disputed_blocks():
    from specky.lint import Doc, matrix_problems

    bad = Doc("specs/a/b.md", (), _fence("inputs:\n  x: bogus\nexpect:\n  y: number"))
    undeclared = Doc(
        "specs/a/c.md",
        (),
        _fence(
            "inputs:\n  x: number\nexpect:\n  y: number\nformulas:\n  y = missing + 1\n---\n| R | 1 | 2 |"
        ),
    )
    disputed = Doc(
        "specs/a/d.md",
        (),
        _fence(BLOCK.replace("| 98 | 96 | to_control |", "| 95 | 96 | to_control |")),
    )
    good = Doc("specs/a/e.md", (), _fence(BLOCK))
    docs = [bad, undeclared, disputed, good]
    problems = matrix_problems(docs, {d.path for d in docs})
    by_doc = {p.doc: p.problem for p in problems}
    assert set(by_doc) == {bad.path, undeclared.path, disputed.path}
    assert "missing" in by_doc[undeclared.path]
    assert by_doc[disputed.path] == (
        "matrix #1 row 'Over-billing': price_var states 95, formula gives 98"
    )


# --- doc_tools -------------------------------------------------------------------


def test_matrix_rows_are_acceptance_behaviours(tmp_path, monkeypatch):
    from specky import doc_tools

    doc = tmp_path / "b.md"
    doc.write_text(
        "# Doc\n\n## Acceptance Tests\n\n| Given | When | Then |\n|---|---|---|\n| a | b | c |\n\n"
        + _fence(BLOCK)
    )
    monkeypatch.setattr(
        doc_tools, "resolve_doc", lambda root, path: (doc, "specs/a/b.md")
    )
    rows = [
        r
        for r in doc_tools.doc_behaviours(tmp_path, "specs/a/b.md")
        if r["section"] == "Acceptance Tests"
    ]
    assert [r["id"] for r in rows] == ["AT-1", "AT-2", "AT-3"]
    assert rows[1]["fields"]["Scenario"] == "Over-billing"
    assert rows[1]["fields"]["→ price_var"] == "98"
    assert (
        "expect price_var = 98, qty_var = 96, verdict = to_control" in rows[1]["text"]
    )
