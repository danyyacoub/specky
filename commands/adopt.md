---
description: Import the repo's existing markdown (docs/, adr/, ARCHITECTURE.md…) into specky's docs tree — dry run first, then the real import on your go-ahead.
argument-hint: "[adopt flags, e.g. --only 'wiki/**' --keep]"
allowed-tools: Bash(specky adopt --dry-run:*), Bash(uv run --project:*)
---

# specky adopt

Flags: `$ARGUMENTS`.

1. Run `specky adopt --dry-run $ARGUMENTS` from the repo root (or
   `uv run --project "${CLAUDE_PLUGIN_ROOT}" specky adopt --dry-run $ARGUMENTS` if `specky` isn't on
   PATH). Show what would move where, and every skipped file with its reason.
2. Ask whether to go ahead. The default `--move` runs `git mv` on every file listed. `--keep` copies
   instead, and `--stub` leaves a pointer behind. Nothing is committed either way.
3. Only on a clear yes, run it without `--dry-run`, adding `--yes` if it's over 20 files. Relay its
   next steps (`specky tag`, `specky index`).
