---
description: Write pytest scaffolds from the Given/When/Then acceptance-test tables and ```matrix blocks in the docs.
argument-hint: "[--force]"
allowed-tools: Bash(specky tests:*), Bash(uv run --project:*)
---

# specky tests

Run `specky tests $ARGUMENTS` from the repo root (or
`uv run --project "${CLAUDE_PLUGIN_ROOT}" specky tests $ARGUMENTS`). Report which scaffold files it
wrote or skipped. Without `--force` it never overwrites a scaffold someone has filled in.

A Given/When/Then row becomes a skipped empty test. A ` ```matrix ` block becomes a parametrized
test over the rows the doc states, read from a `tests/spec/test_*.matrix.json` that every run
refreshes (even without `--force`) — so a doc edit reaches the tests without regenerating them.
Offer to wire each block's `evaluate_<name>()` stub to the project's real code. For plain
scenarios, offer to implement the tests for the doc the user cares about, reading the code the
doc's `sources:` names. `specky tests --check` writes nothing and exits 1 when a matrix data file
is behind the docs — suggest it for CI.
