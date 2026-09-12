---
type: feature
tags: [documentation, diagnostics]
---

# Documentation — Stale Doc Detection

## What It Does

Stale doc detection warns readers and maintainers when documentation has fallen behind the code it describes. The feature measures how far out of sync a doc is (in days) by comparing its last change date against the newest change date of all files it covers, then surfaces that as a "N days behind code" badge in the viewer and as advisory warnings in `specky check`.

## How It Works

1. **Index time measurement**: When `specky index` runs, it calculates for each doc two dates from git: when the doc last changed (committer date) and when the newest file it covers last changed (committer date).

2. **Staleness threshold**: A doc is flagged as stale if its code ran ahead of it by more than `stale_after_days` (configurable in `[check]`, default 14 days).

3. **Verdict storage**: The staleness verdict is stored in the index database (`stale_since` and `last_code_change` columns on the `documents` table), so it's computed once and reused everywhere.

4. **Viewer display**: The HTML viewer shows a "N days behind code" chip as the first item in each stale doc's header, paired with a **Stale** facet filter in the sidebar that applies to search results too.

5. **Check reporting**: When `specky check` runs on a pull request, it reports stale docs that cover the changed code as advisory warnings (never as failures).

6. **Scoped measurement**: Only docs with explicit coverage in the index are measured; docs with no `doc_files` entry receive no verdict.

## Outcomes

| State | When | Display |
|-------|------|---------|
| Fresh | Code last changed ≤ 14 days after doc | No badge |
| Stale | Code last changed > 14 days after doc | "N days behind code" chip + searchable |
| Unmeasured | Doc has no `doc_files` coverage | No badge |
| Out-of-range | Stale docs outside the changed code area | Counted as a summary, not listed |

## Acceptance Tests

| Given | When | Then |
|-------|------|------|
| Doc changed 10 days ago, code in it changed 5 days ago | Viewing the doc | No stale badge shown |
| Doc changed 10 days ago, code in it changed 25 days ago | Viewing the doc | "15 days behind code" badge shown |
| Doc changed 10 days ago, code in it changed 24 days ago exactly (14-day threshold) | Viewing the doc | No stale badge (exactly at threshold) |
| Pull request covers files with stale covering docs | Running `specky check` | Advisory report lists up to 10 stale docs by name, then a count for the rest; exit code is 0 |
| Pull request updates a stale doc | Running `specky check` | Doc is not reported as stale (the update is the fix) |
| Repository has many stale docs, but PR covers only fresh code | Running `specky check` | Number of stale docs elsewhere shown as count, not in report about the range |
