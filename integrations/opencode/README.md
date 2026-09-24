# opencode integration

specky needs two things from any agent: the MCP server (read-only queries over the index) and its
skills (`setup`, `find-feature`, `explore-docs`, `document-domain`, `document-commits`). Everything else — the git hook,
`specky index`, `render-html`, `serve` — is plain CLI and identical everywhere.

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

## Skills

opencode reads skills from `.opencode/skills/`, `.claude/skills/` and `.agents/skills/` — all
resolved against the repo, so an installed plugin still needs a copy here. Vendor the shared skills
to `.agents/skills/`, the path opencode shares with Codex and Devin Cloud:

```bash
for s in setup find-feature explore-docs document-domain document-commits; do
  mkdir -p .agents/skills/$s
  curl -fsSL "https://raw.githubusercontent.com/danyyacoub/specky/main/skills/$s/SKILL.md" \
    -o .agents/skills/$s/SKILL.md
done
```

`find-feature` answers "what is this meant to do before I use it?", `explore-docs` answers behaviour
questions from the docs before reading code, and `document-domain` writes the docs themselves.

## Commands

The plugin's slash commands (`/specky:doctor`, `/specky:check`, `/specky:lint`, …) each wrap one
`specky` CLI workflow: they run it, explain the output, and ask before anything that costs AI calls,
moves files or posts. opencode reads commands from `.opencode/commands/`, and its `$ARGUMENTS` works
the same way, so they copy in as they are. They're prefixed `specky-` here, since opencode has no
plugin namespace:

```bash
mkdir -p .opencode/commands
for c in doctor check lint verify-migration adopt index search tags graph sync pr-comment cost tests export setup-diagrams; do
  curl -fsSL "https://raw.githubusercontent.com/danyyacoub/specky/main/commands/$c.md" \
    -o .opencode/commands/specky-$c.md
done
```

opencode ignores the `allowed-tools` line, which is Claude Code's. `specky` has to be on PATH.

## Cheap model for lookups

A skill can't carry a model on opencode — unknown frontmatter fields are ignored, and there is no
field binding a skill to an agent or a model. `[skills] model` in `specky.toml` names a Claude Code
model, so it isn't the lever here either. Two levers exist, and only the command applies without
switching models mid-session:

- **Command** — copy [`commands/specky-lookup.md`](commands/specky-lookup.md) to `.opencode/commands/`
  and run `/specky-lookup <feature>`. Its `model:` applies to that invocation.
- **Agent** — copy [`agents/specky-docs.md`](agents/specky-docs.md) to `.opencode/agents/` and select
  it; its `model:` applies whenever it answers.

```bash
mkdir -p .opencode/commands .opencode/agents
curl -fsSL https://raw.githubusercontent.com/danyyacoub/specky/main/integrations/opencode/commands/specky-lookup.md \
  -o .opencode/commands/specky-lookup.md
curl -fsSL https://raw.githubusercontent.com/danyyacoub/specky/main/integrations/opencode/agents/specky-docs.md \
  -o .opencode/agents/specky-docs.md
```

Both pin `anthropic/claude-haiku-4-5`. Swap in `openai/gpt-5-nano`, or any `provider/model-id` from
`opencode models`, if that's your cheap tier instead.
