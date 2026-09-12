---
type: workflow
tags: [documentation, cli]
---

# Documentation — Acceptance Test Scaffolding

## What It Does

Every generated documentation file carries a `## Acceptance Tests` table listing scenarios as Given/When/Then rows. The `specky tests` command reads those tables, parses each row as a test case, and generates a pytest file with one skipped test function per scenario — no assertions, just a function that names the expected behaviour and waits for you to fill in the proof.

## How It Works

1. **Query the index** — `specky tests` reads docs from the `.specky/index.db` database (the same index used for search), skipping `specs/history/` to avoid duplicating tests across historical commits.

2. **Parse tables** — For each doc, find the `## Acceptance Tests` section and extract its tables. Tables with exactly three columns are parsed by position (Given/When/Then); tables with different headers are matched by column name instead, so the order doesn't matter.

3. **Name scenarios** — Each data row (that isn't a placeholder like "n/a", "–", or "TBD") becomes one test. The test name is derived from the doc path: `specs/domains/feature.md` becomes `test_domains_feature.py`.

4. **Write test files** — Generate pytest scaffolds to `tests/spec/test_<domain>_<topic>.py`. Each test is decorated with `@pytest.mark.skip` and has the scenario as its docstring. No assertion — you fill that in.

5. **Preserve edits** — Existing files are never overwritten unless you pass `--force`. This protects hand-written test bodies while allowing regeneration of untouched stubs.

## Outcomes

| Result | Meaning |
|--------|---------|
| Written | New test file created, or existing file regenerated with `--force` |
| Skipped | File already exists and `--force` was not passed; no changes made |
| Counted | Total scenarios in each file, reported in the summary |

## Acceptance Tests

| Scenario | Given | When | Then |
|----------|-------|------|------|
| Three-column table | Doc has `\| Given \| When \| Then \|` table in Acceptance Tests | `specky tests` runs | One test function per data row, named from Given/When/Then content |
| Named-column table | Doc has `\| Scenario \| Given \| When \| Then (expected) \|` table | `specky tests` runs | Columns matched by header text; test content same as three-column case |
| Placeholder row | Table contains row with "n/a", "TBD", em dash, or similar | `specky tests` runs | Row skipped; no test generated |
| Force regenerate | Test file exists and has been hand-edited | `specky tests --force` runs | File overwritten with fresh scaffold; hand-written tests lost |
| No force | Test file exists and untouched | `specky tests` (no flag) runs | File left alone; skipped in report |
| History excluded | Docs in `specs/history/` carry Acceptance Tests tables | `specky tests` runs | No tests written for history docs; path excluded before parsing |
