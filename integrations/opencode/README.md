# opencode integration

specky needs two things from any agent: the MCP server (read-only queries over the index) and
the `document-domain` skill. Everything else — the git hook, `specky index`, `render-html`,
`serve` — is plain CLI and identical everywhere.

## MCP server

Add to `opencode.json` in the repo you want documented (or `~/.config/opencode/opencode.json`
to have it everywhere):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "specky": {
      "type": "local",
      "command": ["specky-mcp"],
      "enabled": true
    }
  }
}
```

`specky-mcp` is the console script installed by `uv tool install specky` — the same entry point
Claude Code's [.mcp.json](../../.mcp.json) points at. It speaks stdio and answers from
`.specky/index.db`, so run `specky index` at least once first.

Tools exposed, all read-only: `list_domains`, `search_docs`, `read_doc`, `doc_behaviours`,
`search_history` (the docs and their history); `list_features`, `list_workflows`, `list_tags`,
`get_graph`, `commit_info`, `commits_for_doc` (the feature/workflow catalog);
`render_acceptance_table` (pure formatting) and `ping`. The authoritative list is
[`mcp_server.py`](../../src/specky/mcp_server.py).

## Skill

`skills/document-domain/SKILL.md` and `skills/explore-docs/SKILL.md` (answer behaviour questions
from the docs before reading code) are picked up as-is — opencode reads `.claude/skills/` and
`.agents/skills/` alongside its own paths, so a specky checkout on the plugin path needs no
copy. If you'd rather vendor them, copy each to `.agents/skills/<name>/SKILL.md`.
