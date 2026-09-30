---
type: feature
tags: [cli, documentation]
---

# Documentation — Acceptance Test Scaffolding

## What It Does

Every generated documentation file carries a `## Acceptance Tests` table listing scenarios as Given/When/Then rows. The `specky tests` command reads those tables, parses each row as a test case, and generates a pytest file with one skipped test function per scenario — no assertions, just a function that names the expected behaviour and waits for you to fill in the proof. A ` ```matrix ` block in the same section is read too: it becomes one parametrized test per block, run over the rows the doc states. Those rows live in a `.matrix.json` beside the scaffold that every run rewrites, even without `--force`, so a doc edit reaches a test someone already wired; `specky tests --check` fails when that data is behind the docs.

## How It Works

1. **Query the index** — `specky tests` reads docs from the `.specky/index.db` database (the same index used for search), skipping `specs/history/` to avoid duplicating tests across historical commits.

2. **Parse tables** — For each doc, find the `## Acceptance Tests` section and extract its tables. Columns are matched by header text first, so a table's column order doesn't matter and the assertion column can be called `Then (expected)`. A table whose headers aren't recognisable at all is read by position only when it has exactly three columns, and otherwise skipped rather than guessed at.

3. **Name scenarios** — Each data row (that isn't a placeholder like "n/a", "–", or "TBD") becomes one test, named from its `Scenario` column when the table has one and from its Given/When text when it doesn't. The file name comes from the doc path: `specs/domains/feature.md` becomes `test_domains_feature.py`.

4. **Write test files** — Generate pytest scaffolds to `tests/spec/test_<domain>_<topic>.py`. Each test is decorated with `@pytest.mark.skip` and has the scenario as its docstring. No assertion — you fill that in.

5. **Preserve edits** — Existing files are never overwritten unless you pass `--force`. This protects hand-written test bodies while allowing regeneration of untouched stubs.

6. **Refresh matrix data** — Each ` ```matrix ` block's rows (stated inputs and expected outputs, never computed ones) go to `tests/spec/test_<domain>_<topic>.matrix.json`, which the scaffold reads at import time. This file is the doc's, not yours: every run rewrites it when it differs, and a data file whose doc no longer states a matrix is emptied. `--check` compares instead of writing, for CI.

## Outcomes

| Result | Meaning |
|--------|---------|
| Written | New test file created, or existing file regenerated with `--force` |
| Skipped | File already exists and `--force` was not passed; no changes made |
| Counted | Total scenarios in each file, reported in the summary |
| Refreshed | A `.matrix.json` rewritten because the doc's matrix rows changed |

## Acceptance Tests

| Scenario | Given | When | Then |
|----------|-------|------|------|
| Three-column table | Doc has `\| Given \| When \| Then \|` table in Acceptance Tests | `specky tests` runs | One test function per data row, named from Given/When/Then content |
| Named-column table | Doc has `\| Scenario \| Given \| When \| Then (expected) \|` table | `specky tests` runs | Columns matched by header text; test content same as three-column case |
| Placeholder row | Table contains row with "n/a", "TBD", em dash, or similar | `specky tests` runs | Row skipped; no test generated |
| Force regenerate | Test file exists and has been hand-edited | `specky tests --force` runs | File overwritten with fresh scaffold; hand-written tests lost |
| No force | Test file exists and untouched | `specky tests` (no flag) runs | File left alone; skipped in report |
| History excluded | Docs in `specs/history/` carry Acceptance Tests tables | `specky tests` runs | No tests written for history docs; path excluded before parsing |
| Matrix block | Doc's Acceptance Tests has a ` ```matrix ` block with rows | `specky tests` runs | One parametrized skipped test per block, reading its rows from `test_<slug>.matrix.json`; malformed block skipped (lint reports it) |
| Matrix row edited | Scaffold exists and is wired; a row's expected value changes in the doc | `specky tests` runs (no `--force`) | Scaffold left alone; `.matrix.json` rewritten, so the wired test now checks the new value |
| Matrix data stale | A doc's matrix changed since the last `specky tests` | `specky tests --check` runs | Nothing written; the stale data file named; exit 1 |
