---
description: Draw the feature/workflow graph — docs linked by shared tags and related links — as a Mermaid diagram.
allowed-tools: Bash(specky graph:*), Bash(uv run --project:*)
---

# specky graph

Run `specky graph` from the repo root (or `uv run --project "${CLAUDE_PLUGIN_ROOT}" specky graph`).
It needs `specky index` to have run. Show the Mermaid block it prints as-is, so it renders. Then
name any doc with no edges: it shares no tag with anything, which usually means a stray tag
(`/specky:lint` lists them).
