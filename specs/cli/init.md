---
type: workflow
tags: [cli, configuration]
---

# CLI — Init

## What It Does

Configures specky's AI provider and writes `specky.toml` (which is gitignored). The command asks a series of questions about which provider to use, which model, where the API key lives in the environment, and where the docs root is. It can be run interactively on a terminal or scripted with flags for automated setups like CI, Dockerfiles, or cloud agents.

## How It Works

1. **Gather provider details** — Ask or accept from flags which AI provider to use (`--provider`), which model (`--model`), and where the API key lives in the environment (`--api-key-env`). For compatible providers, optionally accept a base URL (`--base-url`).

2. **Set command and docs root** — Ask or accept which local command to run for fallback AI (`--command`), and where the docs root is in the repo if non-standard (`--docs-root`).

3. **Validate before writing** — Test the config against the live API (unless `--no-validate` is used), so a broken config fails before `specky.toml` exists rather than on the first commit.

4. **Write or error** — If all required fields are provided or answered, write `specky.toml`. If any are missing and there is no terminal, error immediately and name every missing flag at once.

## Outcomes

| Setup Context | Command | Behavior |
|---|---|---|
| Human on terminal | `uv run specky init` | Interactive interview, prompts for each field |
| Human with partial answers | `uv run specky init --provider openai --model gpt-4` | Skips answered fields, prompts the rest |
| Scripted with defaults | `uv run specky init --yes` | Takes all defaults, no prompts, validates config |
| Scripted, validation off | `uv run specky init --yes --no-validate` | Takes defaults, skips API test, writes immediately |
| Non-terminal, incomplete flags | `uv run specky init --provider openai` | Errors before writing; lists all missing required flags |
| All conditions | (any path that completes) | `specky.toml` written to repo root |

## Acceptance Tests

| Given | When | Then |
|---|---|---|
| Terminal available, no flags | Run `uv run specky init` | Prompts for provider, model, API key env var, command, and docs root in sequence |
| Terminal available, one flag | Run `uv run specky init --provider anthropic` | Prompts only the remaining fields (model, API key env var, command, docs root) |
| Non-terminal, all required flags | Run `uv run specky init --yes --provider openai --model gpt-4 --api-key-env OPENAI_API_KEY --command none --docs-root specs` | Writes `specky.toml` without prompting |
| Non-terminal, missing required flag | Run `uv run specky init --provider openai` | Errors and displays all missing required flags before any file is written |
| Validation enabled, API reachable | Any complete setup | Config tested against API; `specky.toml` written only if test succeeds |
| Validation enabled, API unreachable | Any complete setup with bad key | Validation fails; error shown; no file written |
| Validation disabled | `uv run specky init --yes --no-validate` | Config written without testing the API |
