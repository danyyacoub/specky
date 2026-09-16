---
type: feature
tags: [cli, documentation, sync]
---

# Cli — Bootstrap

## What It Does
Bootstrap generates initial feature documentation from a repository's source code instead of its commit history. It runs automatically when `specky sync` first encounters a repo with no feature docs, generating domain documentation in one pass rather than slowly accumulating them commit-by-commit.

## How It Works

1. **Detect bootstrap need** — `specky sync` checks whether any feature docs exist in `specs/`; if not, bootstrap runs automatically (unless `--no-bootstrap` is passed).

2. **Identify product and domains** — Makes one call to the AI to scan the codebase and determine what the product is and which domains it contains.

3. **Generate domain docs** — Makes one call per domain to write its `specs/<domain>/<topic>.md` file from the source code as it currently stands.

4. **Write reference docs** — Generates `PRODUCT.md` and `GLOSSARY.md` based on the domains and code just analyzed.

5. **Process recent commits** — After bootstrap completes, `specky sync` walks the last 10 commits to create history entries; these commits are already reflected in the docs just written, so they only earn a `specs/history/` entry, not a rewrite.

6. **Resume on re-run** — Bootstrap is idempotent; domains that already have docs are skipped, `--max-domains` bounds a single run, and re-running picks up any missing domains.

## Outcomes

| Condition | Behavior | Cost |
|-----------|----------|------|
| Fresh repo, no docs | Bootstrap runs automatically, reads code, writes all domains | O(domains): ~2 + N calls, ~$0.18 typical |
| Repo with partial docs | Bootstrap skips existing domain docs, writes missing ones | Only new domains charged |
| Repo with all docs | Bootstrap skipped, goes straight to commit-driven updates | No bootstrap cost; standard incremental flow |
| `--no-bootstrap` flag | Bootstrap is skipped entirely | Incremental commit-driven path only |
| `--batch` flag | Independent calls sent as async batch at half price | Waits up to 1 hour; results cached on re-run |

## Acceptance Tests

| Given | When | Then |
|-------|------|------|
| Fresh repo with no `specs/` | Running `specky sync` | Bootstrap runs automatically, discovers domains, generates `specs/<domain>/*.md`, `PRODUCT.md`, `GLOSSARY.md` from source code |
| Repo with one existing domain doc | Running `specky bootstrap` | Only missing domain docs are written; existing docs are left unchanged |
| Repo with partial docs + `--max-domains 2` | Running `specky bootstrap --max-domains 2` | At most 2 new domain docs are written; re-running bootstrap writes the rest |
| Repo with code at `src/billing/` | Running `specky bootstrap src/billing/` | Bootstrap scopes discovery and docs to that subtree only |
| `specky sync --no-bootstrap` | Running on a fresh repo | Bootstrap is skipped; sync goes straight to commit-driven doc updates (no docs generated) |
| Bootstrap running with `--batch` | Waiting up to 1 hour | Async batch request completes server-side; specky collects cached results; re-running yields the same docs without recomputation |
