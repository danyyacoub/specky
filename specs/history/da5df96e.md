---
sha: da5df96e9fd07173ceee4d892f0adec1dc92b148
---

# Commit da5df96e

- **Date:** 2026-09-21T18:16:02+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Message:** feat(chat): turn the Ask panel into the Spec Assistant

This commit reworks the viewer's side panel from a one-shot "Ask" widget into a full **Spec Assistant**, because the old design both hid the reasoning behind spec drafts (no approval step before writing) and buried answers in long pages.

Explore now leads with a short standalone answer and tucks details behind "Read more," so readers get the fact immediately instead of scanning.

The `draft spec` flow becomes a staged workflow (`spec_draft.py`): the reader approves scope, impact, and acceptance tests at each step, and those approved tests are spliced verbatim into the final draft. It runs over a read-only docs-and-history toolbox (`doc_tools.py`) that the MCP server also exposes, with matching `explore`/`draft_spec` prompts. Draft state lives only in the reader's tab, is re-validated each step, and never touches disk.

Docs and glossary are updated to match the new naming, and CLAUDE.md now notes `specky.toml` is per-machine, so the active provider should be confirmed with `specky doctor`.
