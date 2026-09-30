"""`specky tests` — turn the Given/When/Then tables in specs/ into pytest scaffolds.

Every generated doc carries an `## Acceptance Tests` table (both doc-generation paths ask for one:
see generator.DOC_STYLE_INSTRUCTIONS and the document-domain skill), and until now nothing read it
back. This module parses those rows and writes one skipped test function per row to
`tests/spec/test_<domain>_<topic>.py`, closing the loop from doc to test.

The scaffolds are deliberately `@pytest.mark.skip`ped and contain no assertions. A generated
assertion would either be wrong or be a tautology, and both are worse than an empty test that
names the behaviour and fails to prove it. An existing file is never overwritten without `--force`,
because the whole point is that someone fills these in.

A ```matrix block (see matrix.py) is the exception: its rows state their expected outputs, so each
block becomes a parametrized test that does assert — on the values the doc wrote down, never on
ones specky computed. Those rows live in a `test_<slug>.matrix.json` next to the scaffold, which
every run rewrites: the scaffold is the project's to wire, the data is the doc's to change.

Docs come from the index rather than a `specs/` walk — one query, and the content is what `specky
index` already read. `specs/history/` is skipped by path before anything is parsed: it holds one
doc per commit, so on a repo with thousands of commits that's where the work would otherwise go.
"""

from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

from specky import matrix, paths
from specky.db import connect

OUT_DIR = "tests/spec"

# Cells that mean "no scenario here". Both doc-generation paths are told to keep the Acceptance
# Tests section even when there's nothing testable, so a placeholder row is the expected shape of
# that — not a parse failure. Public because `doc_tools.doc_behaviours` skips the same rows.
PLACEHOLDERS = {"", "-", "–", "—", "n/a", "na", "none", "(none)", "tbd", "todo"}

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
# Split on unescaped pipes only: a cell can contain `\|`.
_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_SEPARATOR = re.compile(r"^[\s|:-]+$")
_MARKDOWN_NOISE = re.compile(r"[`*_]")


@dataclass(frozen=True)
class Scenario:
    given: str
    when: str
    then: str
    name: str = ""


@dataclass(frozen=True)
class Suite:
    doc_path: str
    out_path: str
    scenarios: tuple[Scenario, ...]
    # `(key, block)` per well-formed ```matrix block under Acceptance Tests — the key names the
    # block's test and evaluator (`matrix.block_keys`).
    matrices: tuple[tuple[str, matrix.Matrix], ...] = ()

    @property
    def data_path(self) -> str:
        """The JSON the matrix tests read their rows from: `test_x.py` → `test_x.matrix.json`."""
        return self.out_path.removesuffix(".py") + ".matrix.json"


@dataclass(frozen=True)
class Emitted:
    written: tuple[str, ...]
    skipped: tuple[str, ...]
    # Scenario count per output path, for every suite found — including the ones left alone, so
    # the report can say how big a file it didn't touch.
    counts: dict[str, int]
    # Matrix data files whose rows changed (or were first written) on this run.
    refreshed: tuple[str, ...] = ()

    @property
    def scenarios(self) -> int:
        return sum(self.counts[path] for path in self.written)


def section(content: str, title: str) -> list[str]:
    """The lines under a heading, up to the next heading at the same or a higher level."""
    lines = content.splitlines()
    for i, line in enumerate(lines):
        match = _HEADING.match(line)
        if not match or match.group(2).strip().lower() != title:
            continue
        level = len(match.group(1))
        body = []
        for later in lines[i + 1 :]:
            deeper = _HEADING.match(later)
            if deeper and len(deeper.group(1)) <= level:
                break
            body.append(later)
        return body
    return []


def split_sections(content: str) -> list[tuple[str, list[str]]]:
    """`content` as `(heading text, lines)` pairs, split on `##` headings only.

    The first pair is always the preamble — frontmatter, `# Title`, any lead paragraph — under the
    empty heading `""`. Each later pair keeps its own `##` line as `lines[0]`, so joining every
    pair's lines back together reproduces `content` exactly. Deeper headings stay inside the `##`
    section above them, which is the granularity generator.py replaces a section at: a `###` is
    part of the argument its parent section is making, not a separate one.
    """
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in content.splitlines():
        match = _HEADING.match(line)
        if match and len(match.group(1)) == 2:
            sections.append((match.group(2).strip(), [line]))
        else:
            sections[-1][1].append(line)
    return sections


def cells(line: str) -> list[str]:
    """One markdown table row's cells, `\\|` unescaped."""
    parts = [c.replace(r"\|", "|").strip() for c in _CELL_SPLIT.split(line.strip())]
    if parts and not parts[0]:
        parts.pop(0)
    if parts and not parts[-1]:
        parts.pop()
    return parts


def tables(lines: list[str]) -> list[tuple[list[str], list[list[str]]]]:
    """Every `| … |` table in these lines, as (header cells, data rows).

    Public because the Spec Assistant's `doc_tools.doc_behaviours` reads a doc's Outcomes and Edge
    Cases tables with it too — one table parser, so the two can't disagree about what a row is.
    """
    found = []
    i = 0
    while i < len(lines):
        starts = lines[i].strip().startswith("|") and i + 1 < len(lines)
        if starts and _SEPARATOR.match(lines[i + 1]):
            header = cells(lines[i])
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not _SEPARATOR.match(lines[i]):
                    rows.append(cells(lines[i]))
                i += 1
            found.append((header, rows))
        else:
            i += 1
    return found


def _columns(header: list[str]) -> tuple[int, int, int, int | None] | None:
    """`(given, when, then, scenario|None)` column indexes, or None if this isn't a G/W/T table.

    Matched on header text, not position, because the two doc-generation paths write different
    shapes: `| Given | When | Then |` from the per-commit generator, and
    `| Scenario | Given | When | Then (expected) |` from the document-domain skill. A bare
    three-column table is read positionally, which is the only guess worth making.
    """
    norm = [_MARKDOWN_NOISE.sub("", h).strip().lower() for h in header]

    def find(word: str) -> int | None:
        return next((i for i, h in enumerate(norm) if h.startswith(word)), None)

    given, when, then = find("given"), find("when"), find("then")
    if given is not None and when is not None and then is not None:
        return given, when, then, find("scenario")
    if len(header) == 3:
        return 0, 1, 2, None
    return None


def _clean(cell: str) -> str:
    return " ".join(cell.split())


def parse_scenarios(content: str) -> list[Scenario]:
    """Every usable row of every Given/When/Then table under `## Acceptance Tests`."""
    scenarios = []
    for header, rows in tables(section(content, "acceptance tests")):
        columns = _columns(header)
        if columns is None:
            continue
        given_i, when_i, then_i, name_i = columns
        for row in rows:
            if len(row) <= max(given_i, when_i, then_i):
                continue  # a ragged row: fewer cells than the header promised
            given, when, then = (_clean(row[i]) for i in (given_i, when_i, then_i))
            # `then` is the assertion. A row without one names no behaviour to pin down, which is
            # what the "nothing testable here" placeholder row looks like.
            if _MARKDOWN_NOISE.sub("", then).lower() in PLACEHOLDERS:
                continue
            name = _clean(row[name_i]) if name_i is not None and len(row) > name_i else ""
            scenarios.append(Scenario(given=given, when=when, then=then, name=name))
    return scenarios


def parse_matrices(content: str) -> list[tuple[str, matrix.Matrix]]:
    """`(key, block)` for every ```matrix block under `## Acceptance Tests` that has rows.

    Blocks stay separate: two matrices may declare different columns and describe different
    code, so each becomes its own parametrized test against its own evaluator. A block that
    doesn't parse is skipped — `specky lint` is where that gets reported, and a broken matrix
    yields no test cases rather than a wrong one.
    """
    blocks = [m for m in matrix.parse_blocks("\n".join(section(content, "acceptance tests"))) if m.rows]
    return list(zip(matrix.block_keys(blocks), blocks))


def matrix_data(suite: Suite) -> str:
    """The rows every matrix test in `suite` reads, as the JSON file's text.

    Stated values only — inputs and the expected outputs the doc writes down, never what a
    formula computes. Numbers are floats, a missing value is null, and a column a row leaves
    blank is absent from that row's `expected`, so the test doesn't assert on it.
    """
    blocks = {
        key: {
            "inputs": [c.name for c in m.inputs],
            "expect": [c.name for c in m.expect],
            "rows": [
                {
                    "label": row.label,
                    "inputs": {c.name: v for c, v in zip(m.inputs, row.inputs)},
                    "expected": dict(row.expected),
                }
                for row in m.rows
            ],
        }
        for key, m in suite.matrices
    }
    return json.dumps({"doc": suite.doc_path, "blocks": blocks}, indent=2, ensure_ascii=False) + "\n"


def out_path_for(doc_path: str) -> str:
    """`specs/cli/cost.md` → `tests/spec/test_cli_cost.py`."""
    # The leading segment is the docs root, whatever it's called; the test name is about the doc.
    stem = doc_path.split("/", 1)[-1].removesuffix(".md")
    slug = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
    return f"{OUT_DIR}/test_{slug}.py"


def _func_name(scenario: Scenario, taken: set[str]) -> str:
    basis = scenario.name or f"{scenario.given} {scenario.when}"
    words = [w for w in re.split(r"[^a-z0-9]+", basis.lower()) if w][:10]
    name = f"test_{'_'.join(words)}"[:80].rstrip("_") if words else "test_scenario"
    candidate, n = name, 2
    while candidate in taken:
        candidate, n = f"{name}_{n}", n + 1
    taken.add(candidate)
    return candidate


def _docstring(scenario: Scenario) -> str:
    """The scenario as the test's docstring — the spec text, verbatim apart from what would end
    the docstring early."""
    lines = []
    labelled = (("Given", scenario.given), ("When", scenario.when), ("Then", scenario.then))
    for label, text in labelled:
        if not text:
            continue
        safe = text.replace("\\", "\\\\").replace('"""', "'''")
        lines += textwrap.wrap(
            f"{label}: {safe}", width=92, initial_indent="    ", subsequent_indent="        "
        ) or [f"    {label}:"]
    return '    """\n' + "\n".join(lines) + '\n    """'


_MATRIX_PRELUDE = '''

# The ```matrix rows, read from the doc: `specky tests` rewrites this JSON on every run (the test
# functions below are yours and are never overwritten), so a doc edit reaches these tests without
# regenerating them. `specky tests --check` fails when the JSON is behind the docs.
MATRIX = json.loads(Path(__file__).with_name("{data_name}").read_text())["blocks"]


def _assert_row(row: dict, got: dict) -> None:
    for key, want in row["expected"].items():
        have = got[key]
        if isinstance(want, float) and not isinstance(have, bool):
            assert have == pytest.approx(want), f"{{row['label']}}: {{key}}"
        else:
            assert have == want, f"{{row['label']}}: {{key}}"'''

_MATRIX_TEST = '''


def evaluate_{key}(inputs: dict) -> dict:
    """The code this block describes: `inputs` has {inputs}; return at least {expect}."""
    raise NotImplementedError("wire this to the code under test")


@pytest.mark.skip(reason="scaffold from {doc_path} — wire `evaluate_{key}` to the project code")
@pytest.mark.parametrize("row", MATRIX["{key}"]["rows"], ids=lambda row: row["label"])
def test_{key}(row):
    _assert_row(row, evaluate_{key}(row["inputs"]))'''


def render_suite(suite: Suite) -> str:
    header = (
        f'"""Acceptance-test scaffolds for {suite.doc_path}.\n\n'
        "Generated by `specky tests`: one function per row of the doc's Given/When/Then table,\n"
        "and one parametrized test per ```matrix block, run against the values each row states.\n"
        "Every test is skipped and empty on purpose: the doc says what the behaviour is, this\n"
        "file is where it gets proved. Drop the skip marker as you implement each one —\n"
        "`specky tests` won't overwrite this file again unless you pass --force.\n"
        '"""\n\n'
    )
    if suite.matrices:
        header += "import json\nfrom pathlib import Path\n\nimport pytest"
        header += _MATRIX_PRELUDE.format(data_name=Path(suite.data_path).name)
    else:
        header += "import pytest"
    taken: set[str] = set()
    blocks = [
        f'\n\n\n@pytest.mark.skip(reason="scaffold from {suite.doc_path}")\n'
        f"def {_func_name(scenario, taken)}():\n{_docstring(scenario)}"
        for scenario in suite.scenarios
    ]
    blocks += [
        _MATRIX_TEST.format(
            key=key,
            doc_path=suite.doc_path,
            inputs=", ".join(c.name for c in m.inputs),
            expect=", ".join(c.name for c in m.expect),
        )
        for key, m in suite.matrices
    ]
    return header + "".join(blocks) + "\n"


def collect(repo_root: Path) -> list[Suite]:
    """One suite per indexed doc that has usable acceptance-test rows."""
    conn = connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT path, content FROM documents WHERE path NOT LIKE ? ORDER BY path",
            (f"{paths.history_prefix(repo_root)}%",),
        ).fetchall()
    finally:
        conn.close()
    suites = []
    for path, content in rows:
        scenarios = parse_scenarios(content)
        matrices = parse_matrices(content)
        if scenarios or matrices:
            suites.append(Suite(path, out_path_for(path), tuple(scenarios), tuple(matrices)))
    return suites


def matrix_files(repo_root: Path, suites: list[Suite]) -> dict[str, str]:
    """Every matrix data file's path and the text it should hold now.

    A doc's blocks, plus an emptied file for any data file already on disk whose doc no longer
    states a matrix — its tests then fail with a KeyError on the block they read, which is the
    right signal: the doc stopped promising what the test checks.
    """
    wanted = {s.data_path: matrix_data(s) for s in suites if s.matrices}
    for existing in sorted((repo_root / OUT_DIR).glob("*.matrix.json")):
        rel = f"{OUT_DIR}/{existing.name}"
        if rel in wanted:
            continue
        try:
            doc = json.loads(existing.read_text()).get("doc", "")
        except (OSError, ValueError):
            doc = ""
        wanted[rel] = json.dumps({"doc": doc, "blocks": {}}, indent=2) + "\n"
    return wanted


def stale_matrix_files(repo_root: Path) -> list[str]:
    """Matrix data files that are missing or behind the docs — `specky tests --check`."""
    stale = []
    for rel, text in matrix_files(repo_root, collect(repo_root)).items():
        target = repo_root / rel
        if not target.exists() or target.read_text() != text:
            stale.append(rel)
    return stale


def emit(repo_root: Path, force: bool = False) -> Emitted:
    """Write scaffolds that don't exist yet (all of them with `force`), and refresh every matrix
    data file regardless — the data is the doc's, never hand-edited, so it is always safe to
    rewrite, and rewriting it is how a doc edit reaches a test someone already wired."""
    written, skipped, refreshed = [], [], []
    suites = collect(repo_root)
    if suites:
        (repo_root / OUT_DIR).mkdir(parents=True, exist_ok=True)
    for suite in suites:
        target = repo_root / suite.out_path
        if target.exists() and not force:
            skipped.append(suite.out_path)
            continue
        target.write_text(render_suite(suite))
        written.append(suite.out_path)
    for rel, text in matrix_files(repo_root, suites).items():
        target = repo_root / rel
        if not target.exists() or target.read_text() != text:
            target.write_text(text)
            refreshed.append(rel)
    return Emitted(
        written=tuple(written),
        skipped=tuple(skipped),
        counts={
            s.out_path: len(s.scenarios) + sum(len(m.rows) for _, m in s.matrices)
            for s in suites
        },
        refreshed=tuple(refreshed),
    )


def report_lines(result: Emitted) -> list[str]:
    if not result.counts:
        return [
            "no `## Acceptance Tests` tables found in the index — run `specky index`, or check "
            "that your docs have that section"
        ]
    lines = [f"wrote {path} ({result.counts[path]} scenario(s))" for path in result.written]
    lines += [
        f"kept {path} ({result.counts[path]} scenario(s)) — pass --force to regenerate it"
        for path in result.skipped
    ]
    lines.append(
        f"{len(result.written)} file(s) written, {result.scenarios} scenario(s); "
        f"{len(result.skipped)} left alone"
    )
    lines += [f"refreshed {path} from the doc's ```matrix rows" for path in result.refreshed]
    return lines
