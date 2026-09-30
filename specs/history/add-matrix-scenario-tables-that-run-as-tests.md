---
commits: [86954aae0ea324b9a3dbf81d74c39d6bd1b13b45]
branch: main
impact: feature
features: [specs/rendering/matrix-tables.md]
---

# Docs can now define input→output scenario tables that run as parametrized tests

- **Date:** 2026-09-30T11:27:25+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Message:** feat(rendering): add ```matrix scenario tables that run as tests

## What changed

Docs can include a ```matrix block declaring typed inputs, expected outputs and optional formulas, one row per case. `specky tests` turns each block into a parametrized test reading rows from a `.matrix.json` refreshed on every run (even without `--force`), and `specky tests --check` writes nothing and exits 1 when that data is behind the docs. Given/When/Then rows still become skipped empty tests as before.

## Why

Rows that state their inputs and expected outputs are independent statements of the answer, whereas formula-derived rows are confidently wrong whenever the formula is and would pin that on the code; keeping the generated data refreshed lets a doc edit reach an already-wired test without regenerating it.
