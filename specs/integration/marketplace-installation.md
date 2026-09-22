---
type: workflow
tags: [configuration, adoption]
---

# Integration — Marketplace Installation

## What It Does

Gets specky from its public GitHub repo into Claude Code, and then into one repo. The repo is its
own single-plugin marketplace (`.claude-plugin/marketplace.json`, `source: "./"`), so there is no
separate registry to publish to. The plugin brings the MCP server, the skills and a commit hook.
The CLI that the git hooks call is a separate install from PyPI.

Installing the plugin doesn't opt any repo in. The plugin is enabled for every repo the user opens,
so everything it runs checks first that the repo chose specky: the commit hook waits for a
`specky.toml`, and the MCP server waits for docs. A repo opts in through the `setup` skill, which
runs `specky init`.

## How It Works

1. **Install the CLI** — `uv tool install specky` puts `specky` and `specky-mcp` on PATH. The git
   hooks record the absolute path of this binary, which is why it has to be a global install and
   not the plugin's own copy: that copy lives under a versioned cache directory that moves on every
   plugin update. `specky --version` reports the installed version.
2. **Add the marketplace** — `claude plugin marketplace add danyyacoub/specky`. Claude Code reads
   `.claude-plugin/marketplace.json` from the repo and lists the one plugin in it.
3. **Install the plugin** — `claude plugin install specky@specky`. Claude Code copies the repo into
   `~/.claude/plugins/cache/specky/specky/<version>/` and loads `skills/`, `hooks/hooks.json` and
   `.mcp.json` from that copy.
4. **Start the MCP server** — On session start, `uv run --project ${CLAUDE_PLUGIN_ROOT} specky-mcp`
   builds an environment for the cached copy (on first use, from `uv.lock`) and serves over stdio,
   in the session's working directory. It reports its version, and its connect-time instructions
   depend on whether that repo has docs (see [chat/mcp-host-guidance.md](../chat/mcp-host-guidance.md)):
   with docs it sends the full guidance, without them it tells the model to leave specky's tools
   alone.
5. **Opt a repo in** — `/specky:setup` checks the CLI is there, runs `specky init` for the chosen
   provider (which writes `specky.toml` and adds it to `.gitignore`), records which model the skills
   run on in its `[skills]` table (the session's unless the user names one), installs the git hooks
   with the user's go-ahead, and runs `specky index`.
6. **Update** — Claude Code offers an update only when `.claude-plugin/plugin.json` changes its
   `version`, so a release bumps it together with `specky.__version__`. The release workflow
   refuses a tag where the two disagree. The CLI updates separately, with `uv tool upgrade specky`.

```mermaid
flowchart TD
    A[uv tool install specky] --> B[claude plugin marketplace add danyyacoub/specky]
    B --> C{marketplace.json valid?}
    C -->|No| D[Add fails with the validation error]
    C -->|Yes| E[claude plugin install specky@specky]
    E --> F[Repo copied into the versioned plugin cache]
    F --> G[Skills, hook and MCP server load in every repo]
    G --> H{Repo has specky.toml / docs?}
    H -->|No| I[Hook exits at once; MCP tells the model to leave its tools alone]
    H -->|Yes| J[Commits documented; MCP answers from the docs]
    I --> K[/specky:setup runs init, hooks, index]
    K --> J
```

## Outcomes

| Scenario | Outcome |
|----------|---------|
| Marketplace added and plugin installed | The skills are listed as `/specky:setup`, `/specky:find-feature`, `/specky:document-domain`, `/specky:explore-docs`, `/specky:launch-viewer`, and the `specky` MCP server connects |
| A repo that never ran `specky init` | Nothing happens in it: no commit docs, no `.specky/`, no output after commits |
| `/specky:setup` finished | `specky.toml` exists, is gitignored and names the skills' model, the three git hooks are installed, and the index is built |
| A new version released | Plugin users are offered it once `plugin.json`'s `version` changes; CLI users get it with `uv tool upgrade specky` |

## Edge Cases

| Situation | What happens | Why |
|---|---|---|
| The CLI isn't installed, only the plugin | The MCP server and skills still work through the plugin's own copy; the setup skill offers `uv tool install specky` before installing hooks | Git hooks run outside Claude Code and need a binary at a path that survives plugin updates |
| `uv` isn't installed | The MCP server fails to start, and the setup skill stops and points at uv's install page | The plugin runs its Python package through uv; it doesn't install uv itself |
| The first session after an install or update | The MCP server starts once uv has built the cached copy's environment; later sessions reuse it | The environment lives in the versioned cache directory, so each new version builds its own |
| The marketplace is added from a local checkout (`claude plugin marketplace add /path/to/specky`) | The plugin loads in place from the checkout instead of being copied | A local-directory marketplace is how specky itself is developed: edits apply without reinstalling |
| The plugin definition is malformed | `claude plugin validate .` rejects it; `tests/test_packaging.py` also checks the versions agree, the skills have frontmatter, and the hook scripts are executable | A manifest that parses but describes nothing installable fails far from the mistake |
| specky is already installed | Re-adding the marketplace or reinstalling is idempotent or says so | Re-running an install command is the ordinary response to an unclear first run |

## Acceptance Tests

| Given | When | Then |
|-------|------|------|
| The public repo | `claude plugin marketplace add danyyacoub/specky` then `claude plugin install specky@specky` | Installation completes, and `/specky:setup` is listed |
| `.claude-plugin/plugin.json` and `specky.__version__` | `tests/test_packaging.py` runs | They are equal, and `CHANGELOG.md` has a section for that version |
| `marketplace.json` | `tests/test_packaging.py` runs | It lists exactly one plugin, named as in `plugin.json`, with `source: "./"` and no second copy of the version |
| A `v*` tag whose version differs from `plugin.json` | The release workflow runs | It fails before building, naming the mismatch |
| The plugin installed and a repo with no `specky.toml` | Claude runs `git commit` there | Nothing is documented and no `.specky/` appears |
| A repo with no docs | The MCP server starts there | Its instructions tell the model to leave specky's tools alone and name `/specky:setup` |
