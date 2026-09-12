---
type: feature
tags: [cli, documentation]
---

# Cli — Pr Comment

## What It Does

When a pull request changes documentation files, reviewers can see from the file list that `specs/billing/refund-flow.md` changed but not what the product now does differently. `specky pr-comment` summarizes a range's doc changes as markdown (added, updated, removed docs with their one-line purposes), then prints it to stdout so you can review it before posting it as a comment via `gh pr comment`.

## How It Works

1. **Invoke with a range**: Run `specky pr-comment --base origin/main` (or `--since` for date/tag) to analyze commits against a baseline.

2. **Collect changed docs**: Git diff identifies which `specs/` files were added, modified, or deleted across the range.

3. **Categorize by type**: History docs (under `specs/history/`) are counted as a group; feature docs are listed separately; index docs (MODULES.md, GLOSSARY.md, etc. at root level) are grouped on one line.

4. **Lookup purposes**: For each feature doc, read its one-line purpose from `MODULES.md`.

5. **Diff `## What It Does`** (first 10 docs only): Extract and show the diff of the `## What It Does` section for updated docs — the part written for someone who doesn't read code.

6. **Bound the output**: Cap doc lists at 30 items, limit detailed diffs to 10 docs (one `git show` each), trim the full body at 55,000 characters on a line boundary.

7. **Print to stdout**: Output goes to stdout with no prefix so you can read it before piping to `gh pr comment --body-file -`.

## Outcomes

| Scenario | Behavior |
|----------|----------|
| No docs changed | Empty or minimal output |
| Few docs changed | All docs listed with purposes; diffs shown for updated docs |
| Many docs added/updated | Lists cut off at 30 with count of remaining; only first 10 diffs included |
| Body exceeds 55k chars | Trimmed at line boundary; note appended saying what was cut |
| History-heavy branch (e.g., 300 commits) | ~300 `specs/history/` docs counted as "… and N more" instead of listed |

## Acceptance Tests

| Given | When | Then |
|-------|------|------|
| Branch with 3 new docs, all documented in MODULES.md | `specky pr-comment --base main` | Added docs listed with their purposes; no diffs shown (new docs have no "before") |
| Branch updating `specs/billing/refund-flow.md` | `specky pr-comment --base main` | Doc listed as updated; `## What It Does` section diff shown with `-`/`+` lines |
| Branch with 50 changed docs | `specky pr-comment --base main` | Only 30 listed; output ends with "… and 20 more" or similar |
| Branch with 300+ history docs added | `specky pr-comment --base main` | History group shows "… and ~300 more" (counted, not listed) |
| Output would exceed 55k chars | `specky pr-comment --base main` | Body trimmed on line boundary; footer notes trim occurred |
| Range includes only `MODULES.md` changes | `specky pr-comment --base main` | MODULES.md listed as updated; no product-behavior summary (index docs grouped) |
| User pipes output to `gh pr comment` | `specky pr-comment --base main \| gh pr comment --body-file -` | Comment posted with zero intervention; output reviewed locally first |
