---
description: Write pytest scaffolds from the Given/When/Then acceptance-test tables in the docs.
argument-hint: "[--force]"
allowed-tools: Bash(specky tests:*), Bash(uv run --project:*)
---

# specky tests

Run `specky tests $ARGUMENTS` from the repo root (or
`uv run --project "${CLAUDE_PLUGIN_ROOT}" specky tests $ARGUMENTS`). Report which scaffold files it
wrote or skipped. Without `--force` it never overwrites a scaffold someone has filled in. Offer to
implement the tests for the doc the user cares about, reading the code the doc's `sources:` names.
