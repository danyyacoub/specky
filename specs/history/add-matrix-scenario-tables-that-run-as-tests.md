---
commits: [86954aae0ea324b9a3dbf81d74c39d6bd1b13b45, 091fadbcb35487ceac59bba0843b2451adf98b3a, 3372b4f4594b69f58e692eb2d020c27ba6a4cfe5, b09bcde09a6396dd92ff2898fb0a5f1100520c62, 3437ecd376e081a909520e776f496cec8fa533d1, e5eb576b67661c2959cf599f1774fd244cc8f0aa, a55aa8c006a4aa35894e9984c3111747c3db9694, 1baa8418e0922cecfde88000866dfac4ee259dbd]
branch: main
impact: feature
features: [specs/rendering/matrix-tables.md, specs/rendering/html-viewer-shell.md, specs/rendering/home-activity-brief.md, specs/documentation/auto-commit-docs.md, specs/documentation/business-logic-filtering.md]
---

# Home page digest groups each shipped change under its feature, window now a week

- **Date:** 2026-09-30T11:27:25+02:00 → 2026-09-30T17:22:39+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Commits:**
    - `86954aae` feat(rendering): add ```matrix scenario tables that run as tests
    - `091fadbc` feat(viewer): sort workflows before features in each sidebar domain
    - `3372b4f4` feat(rendering): brief recent activity from the docs when git history is synthetic
    - `b09bcde0` chore: release 0.2.0
    - `3437ecd3` feat(history): never document skipped, bot, or ignored commits
    - `e5eb576b` chore: release 0.2.1
    - `a55aa8c0` feat(history): document only commits that change business logic
    - `1baa8418` feat(rendering): show what changed, by feature, on the home page

## What changed

The viewer's Recent activity section on the home page now opens with a digest of every shipped change, grouped under the feature doc it is about (a change whose doc names no feature with a page falls under Other changes, last, ordered by each feature's newest change and newest first, with a change listed once and '+N more' past 5), showing its headline and What changed paragraph, author, time and pull request; the per-person collapsed rows still follow under By person. The activity window now defaults to 7 days (was 14), still overridden by `[activity] days`. This builds on the release's earlier additions: ```matrix scenario tables that run as parametrized tests (`specky tests`, `--check` writes nothing and exits 1 when stale), workflows-before-features sidebar ordering with internal-history pages dropping out of the sidebar, and version 0.2.1 across package and plugin manifest. The bigger earlier change stands too: specky documents only commits that change business logic — non-business commits (tests, docs, CI, infra, dependency and version bumps, editor and agent config) are never pending and cost no AI call, with `[history] paths` narrowing and `[history] exclude_paths` adding globs (`!glob` re-includes), and any other commit can answer `{"skip": true}` on the first micro-doc call to stop before classification, leave any extended entry untouched and record the commit in `specs/history/skipped.txt` so it is never asked about again, re-queued only by deleting that line and carried along by amend or rebase. Bracketed skip tags, `[bot]`-authored commits and `[history] ignore` subjects still never appear in pending_commits, the hook, `specky sync`, `specky check`, `specky doctor` or the activity brief, and `specky sync`'s estimate remains one to three AI calls per commit. Commits with no business file cost no AI call.

## Why

The brief listed only headlines behind one collapsed row per person, so getting an overview of the week meant opening every row and still not seeing what any change did; the digest puts each change's What changed paragraph under its feature, and a week is the natural "what changed since last week" window. The same release also cut AI calls and manual cleanup, since most commits had come back as internal entries that had to be deleted by hand.
