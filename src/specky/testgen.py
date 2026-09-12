"""`specky tests` — turn the Given/When/Then tables in specs/ into pytest scaffolds.

Every generated doc carries an `## Acceptance Tests` table (both doc-generation paths ask for one:
see generator.DOC_STYLE_INSTRUCTIONS and the document-domain skill), and until now nothing read it
back. This module parses those rows and writes one skipped test function per row to
`tests/spec/test_<domain>_<topic>.py`, closing the loop from doc to test.

The scaffolds are deliberately `@pytest.mark.skip`ped and contain no assertions. A generated
assertion would either be wrong or be a tautology, and both are worse than an empty test that
names the behaviour and fails to prove it. An existing file is never overwritten without `--force`,
because the whole point is that someone fills these in.

Docs come from the index rather than a `specs/` walk — one query, and the content is what `specky
index` already read. `specs/history/` is skipped by path before anything is parsed: it holds one
doc per commit, so on a repo with thousands of commits that's where the work would otherwise go.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

from specky.db import connect

OUT_DIR = "tests/spec"

# Cells that mean "no scenario here". Both doc-generation paths are told to keep the Acceptance
# Tests section even when there's nothing testable, so a placeholder row is the expected shape of
# that — not a parse failure.
_PLACEHOLDERS = {"", "-", "–", "—", "n/a", "na", "none", "(none)", "tbd", "todo"}

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


@dataclass(frozen=True)
class Emitted:
    written: tuple[str, ...]
    skipped: tuple[str, ...]
    # Scenario count per output path, for every suite found — including the ones left alone, so
    # the report can say how big a file it didn't touch.
    counts: dict[str, int]

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


def _cells(line: str) -> list[str]:
    parts = [c.replace(r"\|", "|").strip() for c in _CELL_SPLIT.split(line.strip())]
    if parts and not parts[0]:
        parts.pop(0)
    if parts and not parts[-1]:
        parts.pop()
    return parts


def _tables(lines: list[str]) -> list[tuple[list[str], list[list[str]]]]:
    """Every `| … |` table in these lines, as (header cells, data rows)."""
    tables = []
    i = 0
    while i < len(lines):
        starts = lines[i].strip().startswith("|") and i + 1 < len(lines)
        if starts and _SEPARATOR.match(lines[i + 1]):
            header = _cells(lines[i])
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not _SEPARATOR.match(lines[i]):
                    rows.append(_cells(lines[i]))
                i += 1
            tables.append((header, rows))
        else:
            i += 1
    return tables


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
    for header, rows in _tables(section(content, "acceptance tests")):
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
            if _MARKDOWN_NOISE.sub("", then).lower() in _PLACEHOLDERS:
                continue
            name = _clean(row[name_i]) if name_i is not None and len(row) > name_i else ""
            scenarios.append(Scenario(given=given, when=when, then=then, name=name))
    return scenarios


def out_path_for(doc_path: str) -> str:
    """`specs/cli/cost.md` → `tests/spec/test_cli_cost.py`."""
    stem = re.sub(r"^specs/", "", doc_path).removesuffix(".md")
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


def render_suite(suite: Suite) -> str:
    header = (
        f'"""Acceptance-test scaffolds for {suite.doc_path}.\n\n'
        "Generated by `specky tests`, one function per row of that doc's Given/When/Then table.\n"
        "Every test is skipped and empty on purpose: the doc says what the behaviour is, this\n"
        "file is where it gets proved. Drop the skip marker as you implement each one —\n"
        "`specky tests` won't overwrite this file again unless you pass --force.\n"
        '"""\n\nimport pytest'
    )
    taken: set[str] = set()
    blocks = [
        f'\n\n\n@pytest.mark.skip(reason="scaffold from {suite.doc_path}")\n'
        f"def {_func_name(scenario, taken)}():\n{_docstring(scenario)}"
        for scenario in suite.scenarios
    ]
    return header + "".join(blocks) + "\n"


def collect(repo_root: Path) -> list[Suite]:
    """One suite per indexed doc that has usable acceptance-test rows."""
    conn = connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT path, content FROM documents WHERE path NOT LIKE 'specs/history/%' "
            "ORDER BY path"
        ).fetchall()
    finally:
        conn.close()
    suites = []
    for path, content in rows:
        scenarios = parse_scenarios(content)
        if scenarios:
            suites.append(Suite(path, out_path_for(path), tuple(scenarios)))
    return suites


def emit(repo_root: Path, force: bool = False) -> Emitted:
    written, skipped = [], []
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
    return Emitted(
        written=tuple(written),
        skipped=tuple(skipped),
        counts={s.out_path: len(s.scenarios) for s in suites},
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
    return lines
