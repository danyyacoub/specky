---
commits: [86954aae0ea324b9a3dbf81d74c39d6bd1b13b45, 091fadbcb35487ceac59bba0843b2451adf98b3a, 3372b4f4594b69f58e692eb2d020c27ba6a4cfe5, b09bcde09a6396dd92ff2898fb0a5f1100520c62]
branch: main
impact: feature
features: [specs/rendering/matrix-tables.md, specs/rendering/html-viewer-shell.md, specs/rendering/home-activity-brief.md]
---

# specky 0.2.0 ships docs-only activity brief, matrix scenarios and sidebar ordering

- **Date:** 2026-09-30T11:27:25+02:00 → 2026-09-30T15:02:15+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Commits:**
    - `86954aae` feat(rendering): add ```matrix scenario tables that run as tests
    - `091fadbc` feat(viewer): sort workflows before features in each sidebar domain
    - `3372b4f4` feat(rendering): brief recent activity from the docs when git history is synthetic
    - `b09bcde0` chore: release 0.2.0

## What changed

The home page's recent-activity brief now copes with a checkout whose git history isn't the repo's — the deployed docs image, where the docs tree is copied into a fresh `git init` of one synthetic commit. Where it previously reported that single commit as the only work ever done, it now reads the history docs themselves: each in-window doc is one change under its recorded author, newest 10 at most, and the header says the answers came from the docs. The same applies when there are docs and no commits at all. Docs can also include a ```matrix block declaring typed inputs, expected outputs and optional formulas, one row per case; `specky tests` turns each block into a parametrized test reading rows from a `.matrix.json` refreshed on every run, and `specky tests --check` writes nothing and exits 1 when that data is behind the docs. In the HTML viewer's sidebar, each domain now groups its docs as workflows first, then features, then untyped docs, each group alphabetical. This release sets the version to 0.2.0 across the package and plugin manifest.

## Why

A docs image loses the git history the brief was built on, so a synthetic commit was being reported as the only work ever done; the docs still record what changed and who wrote it. A domain's workflows are its guided paths, so they lead the sidebar group ahead of the feature reference material, and matrix rows that state their inputs and expected outputs are independent statements of the answer, whereas formula-derived rows are confidently wrong whenever the formula is.
