---
description: Report what specky's AI provider calls have cost — call counts, cache hit rate, characters sent and received.
argument-hint: "[--since <date>]"
allowed-tools: Bash(specky cost:*), Bash(uv run --project:*)
---

# specky cost

Run `specky cost $ARGUMENTS` from the repo root (or
`uv run --project "${CLAUDE_PLUGIN_ROOT}" specky cost $ARGUMENTS`). Summarise the calls per task, the
cache hit rate, and the largest line item. A low cache hit rate on a long `sync` usually means the
prompt prefix changed between calls. Don't pass `--clear-cache` unless the user asks for it.
