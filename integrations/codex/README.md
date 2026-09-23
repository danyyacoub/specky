# Codex integration

specky needs two things from any agent: the MCP server (read-only queries over the index) and its
skills (`setup`, `find-feature`, `explore-docs`, `document-domain`). Everything else — the git hook, `specky index`, `render-html`, `serve` — is plain CLI
and identical everywhere.

Codex discovers skills at `.agents/skills/<name>/SKILL.md` — **not** `.codex/skills/`. That path is
the shared convention, read by opencode and Devin Cloud too, so one vendored copy serves all three.

## MCP server

Add to `.codex/config.toml` in the repo you want documented (or `~/.codex/config.toml` for every
repo):

```toml
[mcp_servers.specky]
command = "specky-mcp"
args = []
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

Codex skill frontmatter carries `name` and `description` only, and the shared skills carry nothing
else, so copy them in as they are:

```bash
for s in setup find-feature explore-docs document-domain; do
  mkdir -p .agents/skills/$s
  curl -fsSL "https://raw.githubusercontent.com/danyyacoub/specky/main/skills/$s/SKILL.md" \
    -o .agents/skills/$s/SKILL.md
done
```

`find-feature` answers "what is this meant to do before I use it?"; `explore-docs` answers broad
"how does X work?" and "why did it change?" questions; `document-domain` writes and updates the docs
themselves.

## Cheap model for lookups

A Codex skill can't carry a model and can't be bound to a subagent, so the skill file is not where a
cheap model attaches. `[skills] model` in `specky.toml` doesn't help either: it names a Claude Code
model, and a skill that can't start a subagent on it runs on the session's model. The lever is a
custom subagent: copy
[`agents/specky-lookup.toml`](agents/specky-lookup.toml) to `.codex/agents/` (project scope, safe to
commit) or `~/.codex/agents/`:

```bash
mkdir -p .codex/agents
curl -fsSL https://raw.githubusercontent.com/danyyacoub/specky/main/integrations/codex/agents/specky-lookup.toml \
  -o .codex/agents/specky-lookup.toml
```

Nothing routes a skill to it automatically — Codex has no skill-to-subagent field — so ask for it:
*"use the `specky-lookup` subagent to tell me what `X` is meant to do"*. Without that, a lookup runs
on the main model.

`model = "gpt-5.6-luna"` (fastest, lowest-cost tier) with `model_reasoning_effort = "low"` are the
cheapest documented values; check both against your Codex version, and see
[`agents/specky-lookup.toml`](agents/specky-lookup.toml) for the rest of the file.
