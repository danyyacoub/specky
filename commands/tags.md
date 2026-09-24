---
description: List the tags docs carry, seed or extend the TAGS.md tag registry (--write), or backfill type/tags on untagged docs (backfill).
argument-hint: "[--write | backfill]"
allowed-tools: Bash(specky tags:*), Bash(specky index:*), Bash(uv run --project:*)
---

# specky tags

Mode: `$ARGUMENTS`. Run from the repo root. If `specky` isn't on PATH, use
`uv run --project "${CLAUDE_PLUGIN_ROOT}" specky` instead.

- **Empty**: run `specky tags` (it reads the index, so run `specky index` first if it has none).
  Show each tag with its doc count, and point out tags carried by one doc only. Those group
  nothing, and are candidates to merge into a shared tag.
- **`--write`**: run `specky tags --write`. It creates or extends `TAGS.md` in the docs root with
  every tag in use. Each new row's meaning is a placeholder naming the docs using the tag. Propose a
  real one-line meaning for each, and a merge for any two tags that name one concept, then ask
  before editing. Once `TAGS.md` exists, `specky lint` and `specky check` flag tags outside it.
- **`backfill`**: `specky tag` asks the configured AI provider to classify every doc missing
  `type:`/`tags:`, one call per doc. Count those docs first (`grep -L '^tags:'` over the docs
  tree), say how many calls that is, and run it only on a clear yes.
