---
commits: [86954aae0ea324b9a3dbf81d74c39d6bd1b13b45, 091fadbcb35487ceac59bba0843b2451adf98b3a, 3372b4f4594b69f58e692eb2d020c27ba6a4cfe5, b09bcde09a6396dd92ff2898fb0a5f1100520c62, 3437ecd376e081a909520e776f496cec8fa533d1, e5eb576b67661c2959cf599f1774fd244cc8f0aa, a55aa8c006a4aa35894e9984c3111747c3db9694]
branch: main
impact: feature
features: [specs/rendering/matrix-tables.md, specs/rendering/html-viewer-shell.md, specs/rendering/home-activity-brief.md, specs/documentation/auto-commit-docs.md, specs/documentation/business-logic-filtering.md]
---

# specky records only commits that change business logic, skipping machine-made and non-product work

- **Date:** 2026-09-30T11:27:25+02:00 → 2026-09-30T17:21:56+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Commits:**
    - `86954aae` feat(rendering): add ```matrix scenario tables that run as tests
    - `091fadbc` feat(viewer): sort workflows before features in each sidebar domain
    - `3372b4f4` feat(rendering): brief recent activity from the docs when git history is synthetic
    - `b09bcde0` chore: release 0.2.0
    - `3437ecd3` feat(history): never document skipped, bot, or ignored commits
    - `e5eb576b` chore: release 0.2.1
    - `a55aa8c0` feat(history): document only commits that change business logic

## What changed

specky now documents a commit only when it changes the product's rules or behaviour. Commits touching no business file — tests, docs, CI, infrastructure, dependency and version bumps, editor and agent config — are never pending and cost no AI call; a built-in list decides which files count, and `[history] paths` narrows it while `[history] exclude_paths` adds globs, with `!glob` to re-include. For any other commit, the first micro-doc call can answer `{"skip": true}`, which stops it before classification and any feature-doc update, leaves an entry it would have extended as it was, and records it in `specs/history/skipped.txt` so it is never asked about again; deleting that line re-queues it, and an amend or rebase carries it along. The `document-commits` skill records a skip with `specky record-commit <sha>` and `{"skip": true}` on stdin. The prompts read only the business files' part of the diff plus a list of those files. This joins the earlier rules that already exclude bracketed skip tags (`[skip specky]`, `[skip ci]`, `[ci skip]`), `[bot]`-authored commits and subjects matching `[history] ignore` globs from `pending_commits`, the hook, `specky sync`, `specky check` and `specky doctor`, all of which now also apply to the activity brief and drop skipped commits entirely while bot commits still count as automated. `specky sync`'s estimate is now one to three AI calls per commit, was two to three. The release also ships the docs-only activity brief for git histories that aren't the repo's, ```matrix scenario tables run as parametrized tests (`specky tests`, with `--check` writing nothing and exiting 1 when stale), workflows-before-features sidebar ordering with internal-history pages dropping out of the sidebar, and version 0.2.1 across package and plugin manifest.

## Why

Every commit cost two or three provider calls, and most came back as `impact: internal` entries (version bumps, tests, terraform, docs plumbing) that then had to be deleted by hand, with a growing list of subject globs to keep them from coming back. A commit touching no business file now costs no call and is never asked about again, so the backlog holds only work that changed what the product does.
