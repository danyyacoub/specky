---
type: feature
tags: [diagnostics, configuration]
---

# Cli — Cost

## What It Does
`specky cost` reports how much you've spent on AI provider calls by showing cache hit rate, character totals, and call counts per command and model. Re-running `specky sync` over commits already documented costs nothing because responses are cached in your repo's index database. The cache holds at most 20 MB of responses to keep your `.specky/` folder reasonable.

## How It Works

1. **Every provider call is cached.** When specky asks the model a question (identified by the model name + prompt hash), the response is stored in `.specky/index.db` and checked before making the same call again.

2. **The cache key includes the model.** If you switch from one model to another, specky re-asks rather than serving the old model's answers, because different models give different responses to the same prompt.

3. **Oldest responses are evicted when the cache exceeds 20 MB.** This keeps your index database bounded and predictable in size, since backfilling over many commits could otherwise write hundreds of megabytes.

4. **Every call (cache hit or miss) is recorded.** A usage row is written to track what was spent, even if the response came from the cache.

5. **`specky cost` groups and reports usage by command and model.** It shows the number of calls, the cache hit rate, and the total characters in prompts and responses sent to and received from your provider since you last cleared the cache.

6. **The cache survives `specky index` rebuilds.** Since responses were paid for, they are not discarded when the index is rebuilt.

## Outcomes

| Outcome | When | What You See |
|---------|------|--------------|
| Cache hit | Identical prompt (same model) seen before | No provider call made; response served from index; usage row updated |
| Cache miss | New prompt or cache cleared | Provider called; response cached and recorded |
| Disabled | `cache = false` in `specky.toml` | Usage still recorded; cache never checked or written |
| SQLite error | Index read-only or corrupt | Call is made to provider anyway; cache layer is skipped |
| Cache full | 20 MB of responses stored | Oldest entries evicted to make room for new ones |

## Acceptance Tests

| Given | When | Then |
|-------|------|------|
| First run of `specky sync` over 10 commits | Command completes | `specky cost` shows 10 calls, 0% cache hit rate |
| Re-run `specky sync` over same 10 commits | Command completes | `specky cost` shows 10 more rows recorded, 100% cache hit rate for this run |
| `specky cost --since 2026-09-01` | User narrows the window | Only usage rows from that date forward are included in hit rate and character totals |
| `specky cost --clear-cache` | User wants to force fresh calls | Cache is emptied; previous usage rows remain in the report |
| `cache = false` in `specky.toml` | `specky sync` runs | Provider is called every time; usage rows still appear in `specky cost` |
| `.specky/index.db` is read-only | `specky sync` runs | Calls are made to provider; no error raised; cache layer silently skipped |
