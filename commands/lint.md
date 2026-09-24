---
description: Check the docs as a set — terms used across docs that the glossary doesn't define, tags outside the vocabulary, numbers two docs disagree on.
argument-hint: "[doc paths or directories, default all]"
allowed-tools: Bash(specky lint:*), Bash(uv run --project:*)
---

# specky lint

Docs to check: `$ARGUMENTS` (empty means every doc).

Run `specky lint $ARGUMENTS` from the repo root. If `specky` isn't on PATH, run
`uv run --project "${CLAUDE_PLUGIN_ROOT}" specky lint $ARGUMENTS`. It's offline and reads the
docs as they are on disk, uncommitted edits included.

Then, for each finding, propose the fix:
- **Undefined term** — a glossary row: the term and a one-sentence definition, taken from how the
  docs use it and checked against the code. If two docs mean different things by it, say so
  instead of picking one.
- **Unregistered or single-doc tag** — the existing tag it should be (from `TAGS.md`, or the tags
  other docs carry). If none fits, propose adding a row to `TAGS.md`.
- **Conflicting numbers** — look up the value in the code, name the doc that should own the rule,
  and propose making the other doc link to it rather than restate it.

Ask before editing any file. Glossary and tag changes affect every doc.
