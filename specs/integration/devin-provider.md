---
type: feature
tags: [ai, configuration, cli]
---

# Integration — Devin Provider

## What It Does

Specky can use Devin CLI as the AI provider that writes and maintains your documentation, instead of an API key or another coding agent. It runs Devin headless and feeds it the diff or doc content to work on, using the Devin login already on the machine.

The same integration also covers Devin Desktop as an MCP host: it starts `specky-mcp` from the user's home directory rather than the project, so the server resolves which repo to answer for per tool call — from `SPECKY_REPO_ROOT`, then the cwd, then the workspace roots the host reports.

## How It Works

1. **Select Devin as the provider** — `specky init` accepts `--provider agent --agent devin`, alongside the other supported agents.
2. **Pass the prompt through a file** — because `devin -p` ignores stdin, the prompt is handed over via `--prompt-file /dev/stdin` rather than piped directly.
3. **Disable the workspace-trust prompt** — `--respect-workspace-trust false` keeps print mode working in a fresh clone or a Devin VM, where the trust prompt can't be shown and would otherwise fail.
4. **Run headless on Devin's own login** — no API key is needed; Devin uses the credentials it already has.
5. **Optionally pin a model** — naming a model pins it; leaving it out keeps Devin's own default.
6. **Resolve the repo per MCP call** — Devin Desktop starts MCP servers from the user's home directory, not the project, so `git rev-parse` in `specky-mcp` finds no repo and every tool but `ping` failed with a generic error. Each tool call now resolves its repo in order: the `SPECKY_REPO_ROOT` env var (expanded, and required to be inside a git repo), then the cwd, then the workspace folders the host reports as MCP roots. A multi-root workspace prefers the candidate that actually has specky set up (`specky.toml` or a docs root); otherwise the first candidate wins. When none of the three is a repo, the tool raises a `ToolError` naming `SPECKY_REPO_ROOT` and what to set, so the message reaches the model instead of the SDK's generic one.
7. **Tell the model what to do with no known repo** — a server started outside a repo now gets `UNKNOWN_REPO_INSTRUCTIONS` rather than `NO_DOCS_INSTRUCTIONS`, pointing at the workspace's own docs (a `specky.toml`, or a `specs/<domain>/<topic>.md` tree) before falling back to leaving the tools alone.
8. **Set the override per machine** — the path differs per machine, so `SPECKY_REPO_ROOT` belongs in the gitignored `.devin/mcp_config.local.json`: `{"mcpServers": {"specky": {"command": "specky-mcp", "args": [], "env": {"SPECKY_REPO_ROOT": "/absolute/path/to/the/repo"}}}}`. Without an override or a reported root, every tool but `ping` returns an error naming `SPECKY_REPO_ROOT`.

## Outcomes

| Situation | Result |
| --- | --- |
| `agent = devin` configured | Specky runs Devin with the prompt passed via `--prompt-file /dev/stdin` |
| Model named in config | That model is appended to the command; Devin uses it |
| Model omitted | Devin keeps its own default model |
| Unknown agent name | Configuration error: the agent "isn't one specky knows" |
| `SPECKY_REPO_ROOT` set for `specky-mcp` | Tools answer for that repo, expanded from `~`; a path outside a git repo is an error naming the variable |
| Server started inside a repo | Tools answer for the cwd's repo, as before |
| Server started elsewhere, host reports roots | Tools use the workspace roots the host reports as MCP roots; a multi-root workspace prefers the repo that has specky set up |
| No override, no cwd repo, no usable roots | Every tool but `ping` returns an error saying the server was started outside a git repo and to set `SPECKY_REPO_ROOT` |
| Host can't report roots, or the request fails | Treated as no roots; resolution falls through to the cwd / override checks |

## Acceptance Tests

| Given | When | Then |
| --- | --- | --- |
| Config `provider=agent, agent=devin, model=swe` | The provider is loaded | Command is `devin -p --prompt-file /dev/stdin --respect-workspace-trust false --model swe` |
| Config `agent=devin` with no model | The provider is loaded | Command uses `--prompt-file /dev/stdin` and `--respect-workspace-trust false`, with no model appended |
| Config names an agent specky doesn't know (e.g. `hal9000`) | The provider is loaded | A `ConfigError` is raised matching "isn't one specky knows" |
| A prompt is piped to Devin via stdin | Devin runs in print mode | The prompt arrives through `--prompt-file`, since `devin -p` ignores stdin |
| `SPECKY_REPO_ROOT` points inside a git repo | Any tool but `ping` is called | The tool answers for that repo's docs and history |
| `SPECKY_REPO_ROOT` points outside any git repo | Any tool but `ping` is called | A `ToolError` says the value "is not inside a git repo" |
| Server started from the user's home directory, no `SPECKY_REPO_ROOT`, host advertises MCP roots | Any tool but `ping` is called | The repo is taken from the reported workspace roots |
| Server started outside a repo with no override and no usable roots | Any tool but `ping` is called | A `ToolError` names `SPECKY_REPO_ROOT` and the cwd, and says how to fix it |
| A server is started with no repo at all | Its instructions are read | The model gets `UNKNOWN_REPO_INSTRUCTIONS`, pointing at the workspace's own specky docs first |
| A workspace switch happens while the server stays up | A later tool call is made | The repo is resolved again per call, not cached from startup |
