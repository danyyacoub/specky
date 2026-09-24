---
description: Keyword search over the docs and git history specky has indexed.
argument-hint: "<query>"
allowed-tools: Bash(specky search:*), Bash(specky index:*), Bash(uv run --project:*)
---

# specky search

Query: `$ARGUMENTS`. If empty, ask what to search for, and stop.

Run `specky search "$ARGUMENTS"` from the repo root (or
`uv run --project "${CLAUDE_PLUGIN_ROOT}" specky search "$ARGUMENTS"`). If it says there's no index
yet, run `specky index` once and search again.

List the hits as doc paths with their titles, best first, and quote the snippet only where it
answers the question. To answer from a doc, open the file itself rather than going by the snippet.
