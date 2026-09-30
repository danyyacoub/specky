---
commits: [86954aae0ea324b9a3dbf81d74c39d6bd1b13b45, 091fadbcb35487ceac59bba0843b2451adf98b3a, 3372b4f4594b69f58e692eb2d020c27ba6a4cfe5, b09bcde09a6396dd92ff2898fb0a5f1100520c62, 3437ecd376e081a909520e776f496cec8fa533d1, e5eb576b67661c2959cf599f1774fd244cc8f0aa]
branch: main
impact: feature
features: [specs/rendering/matrix-tables.md, specs/rendering/html-viewer-shell.md, specs/rendering/home-activity-brief.md, specs/documentation/auto-commit-docs.md]
---

# specky 0.2.1 ships docs-only brief, matrix tests, sidebar order and never-documented commits

- **Date:** 2026-09-30T11:27:25+02:00 → 2026-09-30T16:11:03+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Commits:**
    - `86954aae` feat(rendering): add ```matrix scenario tables that run as tests
    - `091fadbc` feat(viewer): sort workflows before features in each sidebar domain
    - `3372b4f4` feat(rendering): brief recent activity from the docs when git history is synthetic
    - `b09bcde0` chore: release 0.2.0
    - `3437ecd3` feat(history): never document skipped, bot, or ignored commits
    - `e5eb576b` chore: release 0.2.1

## What changed

The home page's recent-activity brief now copes with a checkout whose git history isn't the repo's — the deployed docs image, where the docs tree is copied into a fresh `git init` of one synthetic commit: instead of reporting that commit as the only work ever done, it reads the history docs themselves, one change per in-window doc under its recorded author, at most 10, with the header saying the answers came from the docs (the same when there are docs and no commits at all). Docs can include a ```matrix block declaring typed inputs, expected outputs and optional formulas, one row per case; `specky tests` turns each block into a parametrized test reading rows from a `.matrix.json` refreshed on every run, and `specky tests --check` writes nothing and exits 1 when that data is behind the docs. In the HTML viewer's sidebar, each domain groups workflows first, then features, then untyped docs, alphabetically within each group, and history entries marked `impact: internal` keep their page but drop out of the sidebar. Some commits are never documented at all: one shared rule now excludes bracketed skip tags (`[skip specky]`, `[skip ci]`, `[ci skip]`), `[bot]`-authored commits and subjects matching a new `[history] ignore` glob list in `specky.toml` or `pyproject.toml` from `pending_commits`, the hook, `specky sync`, `specky check` and `specky doctor`, so a doc deleted for such a commit stays deleted; the activity brief drops those skip-tagged and ignored commits entirely, while bot commits still count as automated. This release sets the version to 0.2.1 across the package and plugin manifest.

## Why

A docs image loses the git history the brief was built on, so a synthetic commit was being reported as the only work ever done; the docs still record what changed and who wrote it. A domain's workflows are its guided paths, so they lead the sidebar group ahead of the feature reference material, and matrix rows that state their inputs and expected outputs are independent statements of the answer, whereas formula-derived rows are confidently wrong whenever the formula is. Machine-made and deliberately skipped commits carry nothing worth recording, so one rule keeps them out of the backlog everywhere and lets a deleted doc stay deleted.
