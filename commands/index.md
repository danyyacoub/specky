---
description: Rebuild specky's search index of the docs and git history (offline, no AI call).
allowed-tools: Bash(specky index:*), Bash(uv run --project:*)
---

# specky index

Run `specky index` from the repo root (or `uv run --project "${CLAUDE_PLUGIN_ROOT}" specky index`
if `specky` isn't on PATH). Report the doc and commit counts it prints. It's a full rebuild and
safe to re-run: search, `specky check`, the MCP tools and the viewer all read what it writes.
