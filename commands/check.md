---
description: Run the offline CI doc gate — which docs describing changed code this branch didn't update, plus facts docs stopped stating, glossary gaps and stray tags.
argument-hint: "[base revision, default origin/HEAD]"
allowed-tools: Bash(specky index:*), Bash(specky check:*), Bash(uv run --project:*)
---

# specky check

Base revision: `$ARGUMENTS` (empty means specky's default — `origin/HEAD`, else `HEAD~1`).

Run from the repo root, as one Bash call, adding `--base <that revision>` when one was given:

```bash
specky index && specky check --advisory
```

If `specky` isn't on PATH, use `uv run --project "${CLAUDE_PLUGIN_ROOT}" specky` for both.
`--advisory` prints the same report and exits 0, so the report always reaches you. Say whether
the real gate (without `--advisory`, as CI runs it) would fail.

Then report, most important first:
1. **Violations** — each `code file → doc` line: the doc describes code this range changed and
   wasn't updated. Say what in the doc the change likely made wrong, from the diff
   (`git diff <base>...HEAD -- <code file>`).
2. **Facts removed** — docs that stopped stating a number, formula or defined term. For each, say
   whether the code still applies it (then the doc lost it) or changed it (then it's fine).
3. **Glossary, tags, conflicts** — terms other docs share that the glossary lacks, tags outside the
   vocabulary, numbers two docs disagree on.
4. Everything else in one line each.

Offer to fix the docs. Edit nothing until the user says so.
