---
description: Backfill history docs for commits that don't have one — dry run and cost first, then the real sync on your go-ahead.
argument-hint: "[--since <rev or date>] [--limit N]"
allowed-tools: Bash(specky sync --dry-run:*), Bash(uv run --project:*)
---

# specky sync

Flags: `$ARGUMENTS`.

1. Run `specky sync --dry-run $ARGUMENTS` from the repo root (or
   `uv run --project "${CLAUDE_PLUGIN_ROOT}" specky sync --dry-run $ARGUMENTS`). It calls no
   provider. Report how many commits are undocumented and the estimated AI calls. It's 2–3 per
   commit, so a long backlog costs real money.
2. For a large backlog, suggest narrowing it with `--since <tag or date>` or `--limit N`.
3. Only on a clear yes, run the same command without `--dry-run`. Afterwards, `specky cost` shows
   what it spent.
