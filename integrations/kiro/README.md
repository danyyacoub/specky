# Kiro integration

specky needs two things from any agent: the MCP server (read-only queries over the index) and its
skills (`find-feature`, `explore-docs`, `document-domain`). Everything else — the git hook,
`specky index`, `render-html`, `serve` — is plain CLI and identical everywhere.

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

## Skills

Kiro follows the same open Agent Skills standard as Claude Code's `SKILL.md`, so copy the shared
skills in as they are:

```bash
for s in find-feature explore-docs document-domain; do
  mkdir -p .kiro/skills/$s
  curl -fsSL "https://raw.githubusercontent.com/danyyacoub/specky/main/skills/$s/SKILL.md" \
    -o ".kiro/skills/$s/SKILL.md"
done
```

`find-feature` answers "what is this meant to do before I use it?", `explore-docs` answers behaviour
questions from the docs before reading code, and `document-domain` writes the docs themselves.

## Cheap model for lookups

A Kiro skill has no `model` field, and `[skills] model` in `specky.toml` names a Claude Code model,
so the pin lives on a custom agent. Copy
[`agents/specky-docs.json`](agents/specky-docs.json) to `.kiro/agents/`:

```bash
mkdir -p .kiro/agents
curl -fsSL https://raw.githubusercontent.com/danyyacoub/specky/main/integrations/kiro/agents/specky-docs.json \
  -o .kiro/agents/specky-docs.json
```

Two things to check. **Custom agents load no skills by default**, which is why the file's `resources`
names `skill://.kiro/skills/**/SKILL.md`. And the `model` id must match Kiro's model list — open
`/model` in an active chat and copy the exact id from there (the docs' examples look like
`claude-sonnet-4`, and the cheapest tier is `Qwen3 Coder Next`). A model Kiro doesn't recognise
falls back to the default and shows a warning.
