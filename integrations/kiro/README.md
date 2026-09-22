# Kiro integration

specky needs two things from any agent: the MCP server (read-only queries over the index) and
the `document-domain` skill. Everything else — the git hook, `specky index`, `render-html`,
`serve` — is plain CLI and identical everywhere.

## MCP server

Add to `.kiro/settings/mcp.json` in the repo you want documented (or
`~/.kiro/settings/mcp.json` for every workspace):

```json
{
  "mcpServers": {
    "specky": {
      "command": "specky-mcp",
      "args": [],
      "disabled": false,
      "autoApprove": [
        "ping", "list_domains", "list_features", "list_workflows", "list_tags", "get_graph"
      ]
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

Being read-only is why auto-approving them is safe. The `autoApprove` list above covers only the
ones that take no argument; add the rest if you'd rather not be asked.

## Skill

Kiro follows the same open Agent Skills standard as Claude Code's `SKILL.md`, so copy the
shared skills in. `explore-docs` answers behaviour questions from the docs before reading code:

```bash
for s in document-domain explore-docs; do
  mkdir -p .kiro/skills/$s
  curl -fsSL "https://raw.githubusercontent.com/danyyacoub/specky/main/skills/$s/SKILL.md" \
    -o ".kiro/skills/$s/SKILL.md"
done
```
