---
commits: [86954aae0ea324b9a3dbf81d74c39d6bd1b13b45, 091fadbcb35487ceac59bba0843b2451adf98b3a]
branch: main
impact: improvement
features: [specs/rendering/matrix-tables.md, specs/rendering/html-viewer-shell.md]
---

# Sidebar now lists a domain's workflows before its features

- **Date:** 2026-09-30T11:27:25+02:00 → 2026-09-30T14:59:51+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Commits:**
    - `86954aae` feat(rendering): add ```matrix scenario tables that run as tests
    - `091fadbc` feat(viewer): sort workflows before features in each sidebar domain

## What changed

Docs can include a ```matrix block declaring typed inputs, expected outputs and optional formulas, one row per case. `specky tests` turns each block into a parametrized test reading rows from a `.matrix.json` refreshed on every run (even without `--force`), and `specky tests --check` writes nothing and exits 1 when that data is behind the docs. In the HTML viewer's sidebar, each domain now groups its docs as workflows first, then features, then untyped docs, each group alphabetical; previously all docs in a domain were listed together alphabetically by title.

## Why

Rows that state their inputs and expected outputs are independent statements of the answer, whereas formula-derived rows are confidently wrong whenever the formula is and would pin that on the code; keeping the generated data refreshed lets a doc edit reach an already-wired test without regenerating it. A domain's workflows are its guided paths, so they lead the sidebar group ahead of the feature reference material.
