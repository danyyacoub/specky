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
      "autoApprove": ["ping", "list_features", "list_workflows", "list_tags", "get_graph"]
    }
  }
}
```

`specky-mcp` is the console script installed by `uv tool install specky` — the same entry point
Claude Code's [.mcp.json](../../.mcp.json) points at. It speaks stdio and answers from
`.specky/index.db`, so run `specky index` at least once first.

Tools exposed: `ping`, `list_features`, `list_workflows`, `list_tags`, `get_graph`,
`commit_info`, `commits_for_doc`. All read-only, which is why auto-approving them is safe;
`commit_info`/`commits_for_doc` take an argument, so they're left off that list.

## Skill

Kiro follows the same open Agent Skills standard as Claude Code's `SKILL.md`, so copy the
shared skill in:

```bash
mkdir -p .kiro/skills/document-domain
cp /path/to/specky/skills/document-domain/SKILL.md .kiro/skills/document-domain/
```
