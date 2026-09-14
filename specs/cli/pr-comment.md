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

5. **Show the `## What It Does` section** (first 10 docs only): quoted whole for an added doc, and for an updated one as a `-`/`+` diff of that section — the part written for someone who doesn't read code. An updated doc whose summary didn't move says so, which is different from a doc past the limit that nobody looked at.

6. **Report the doc updates that were refused**: If the post-commit hook generated a doc update and declined to write it — because it would have dropped hand-written content, or named a flag this CLI doesn't accept — those drafts sit in `.specky/pending/`, which is gitignored. They get their own block, louder than the other notes: every other line describes a doc change that happened, and this one describes one that was stopped, on a machine the reviewer can't see.

7. **Bound the output**: Cap doc lists at 30 items, limit detailed diffs to 10 docs (one `git show` each), trim the full body at 55,000 characters on a line boundary.

8. **Print to stdout**: Output goes to stdout with no prefix so you can read it before piping to `gh pr comment --body-file -`.

## Outcomes

| Scenario | Behavior |
|----------|----------|
| No docs changed | One line: `**Docs:** no changes under specs/ since <base>` — honest to post as-is |
| Few docs changed | All docs listed with purposes; summaries quoted or diffed |
| Many docs added/updated | Lists cut off at 30 with a count of the rest; only the first 10 summaries included |
| Body exceeds 55k chars | Trimmed at a line boundary, closing a code fence the cut landed inside, with a note saying it was trimmed |
| History-heavy branch (e.g., 300 commits) | One line: "300 per-commit doc(s) under `specs/history/` not listed" |
| A doc moved domain | Listed under its new path, with "(was `<old path>`)", and its summary diffed against the old path |
| The hook refused a doc update on this machine | A ⚠️ block names the pending drafts and points at `specky doctor`, whether or not the range changed any docs |

## Acceptance Tests

| Given | When | Then |
|-------|------|------|
| Branch with 3 new docs, all documented in MODULES.md | `specky pr-comment --base main` | Added docs listed with their purposes, each with its `## What It Does` quoted as a blockquote |
| Branch updating `specs/billing/refund-flow.md` | `specky pr-comment --base main` | Doc listed as updated; `## What It Does` section diff shown with `-`/`+` lines |
| Branch editing a doc everywhere except its `## What It Does` | `specky pr-comment --base main` | Doc listed as updated, with "Summary unchanged; the rest of the doc was edited" instead of a diff |
| Branch with 50 changed docs | `specky pr-comment --base main` | Only 30 listed; output ends with "…and 20 more." |
| Branch with 300 history docs added | `specky pr-comment --base main` | One line saying 300 per-commit docs weren't listed; no history path appears |
| Output would exceed 55k chars | `specky pr-comment --base main` | Body trimmed on a line boundary with an even number of code fences; note says it was trimmed |
| Range includes only `MODULES.md` changes | `specky pr-comment --base main` | MODULES.md named on the "Index docs updated" line; no summary quoted or diffed for it |
| Target branch changed docs after the fork point | `specky pr-comment --base main` | Those docs are absent — the range is `main...HEAD`, so only this branch's own changes appear |
| A draft waiting in `.specky/pending/`, plus a doc the range did change | `specky pr-comment --base main` | Both appear: the changed doc listed as updated, and a refused-draft block naming the draft and `specky doctor` |
| A draft waiting in `.specky/pending/` and no doc changes in the range | `specky pr-comment --base main` | The "no changes under `specs/`" line, followed by the refused-draft block — the case where silence would be worst |
| Nothing in `.specky/pending/` | `specky pr-comment --base main` | No refused-draft block appears |
| User pipes output to `gh pr comment` | `specky pr-comment --base main \| gh pr comment --body-file -` | specky itself posts nothing; the comment is created by the user's own `gh` command |
