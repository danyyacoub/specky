# Set up specky with your AI agent

specky gives an agent two things: an MCP server that runs read-only queries over the docs index, and
four skills. `setup` configures specky in a repo, `find-feature` looks up what a feature is meant
to do, `explore-docs` answers "how does X work?" from the docs, and `document-domain` writes the
docs. Everything else is plain CLI and
works the same with any agent: the git hooks, `specky index`, `render-html`, `serve` and `check`.

Every agent starts the same way. Install the CLI, then in the repo you want documented:

```bash
uv tool install specky
specky init               # choose a provider; writes specky.toml (gitignored)
specky install-git-hook   # document every commit from now on
specky index              # the MCP server answers from this index
```

## Claude Code

Claude Code is the main target. The plugin bundles the MCP server and the skills, so no manual wiring
is needed:

```bash
claude plugin marketplace add danyyacoub/specky
claude plugin install specky@specky
```

Then run `/specky:setup` in the repo. It does the three `specky` commands above for you. To run the
skills on a lower-cost model, set `[skills] model = "haiku"` in `specky.toml`.

## Other agents

These agents have no plugin, so you add the MCP server and copy in the skills yourself. Each guide
has the exact config and commands. Once the `setup` skill is copied in, asking the agent to "set up
specky" runs the steps above for you, as `/specky:setup` does in Claude Code.

| Agent | MCP server config | Skills folder | Lower-cost model for lookups | Guide |
|---|---|---|---|---|
| Codex | `.codex/config.toml` | `.agents/skills/` | `specky-lookup` subagent | [codex/](codex/README.md) |
| opencode | `opencode.json` | `.agents/skills/` | `/specky-lookup` command or `specky-docs` agent | [opencode/](opencode/README.md) |
| Kiro | `.kiro/settings/mcp.json` | `.kiro/skills/` | `specky-docs` custom agent | [kiro/](kiro/README.md) |
| Devin | `.devin/mcp_config.json` | `AGENTS.md` and a playbook | n/a | [devin/](devin/README.md) |

The MCP server command is always `specky-mcp` with no arguments. The skills come from
[`skills/`](../skills/), and each guide gives a one-line loop that copies them in.

Devin works differently from the others. It runs in a throwaway VM and opens a pull request, so its
guide also covers a blueprint and whether Devin should write docs itself or only read them.
