---
type: feature
tags: [ai, configuration, cli]
---

# Integration — Devin Provider

## What It Does
Specky can use Devin CLI as the AI provider that writes and maintains your documentation, instead of an API key or another coding agent. It runs Devin headless and feeds it the diff or doc content to work on, using the Devin login already on the machine.

## How It Works
1. **Select Devin as the provider** — `specky init` accepts `--provider agent --agent devin`, alongside the other supported agents.
2. **Pass the prompt through a file** — because `devin -p` ignores stdin, the prompt is handed over via `--prompt-file /dev/stdin` rather than piped directly.
3. **Disable the workspace-trust prompt** — `--respect-workspace-trust false` keeps print mode working in a fresh clone or a Devin VM, where the trust prompt can't be shown and would otherwise fail.
4. **Run headless on Devin's own login** — no API key is needed; Devin uses the credentials it already has.
5. **Optionally pin a model** — naming a model pins it; leaving it out keeps Devin's own default.

## Outcomes
| Situation | Result |
| --- | --- |
| `agent = devin` configured | Specky runs Devin with the prompt passed via `--prompt-file /dev/stdin` |
| Model named in config | That model is appended to the command; Devin uses it |
| Model omitted | Devin keeps its own default model |
| Unknown agent name | Configuration error: the agent "isn't one specky knows" |

## Acceptance Tests
| Given | When | Then |
| --- | --- | --- |
| Config `provider=agent, agent=devin, model=swe` | The provider is loaded | Command is `devin -p --prompt-file /dev/stdin --respect-workspace-trust false --model swe` |
| Config `agent=devin` with no model | The provider is loaded | Command uses `--prompt-file /dev/stdin` and `--respect-workspace-trust false`, with no model appended |
| Config names an agent specky doesn't know (e.g. `hal9000`) | The provider is loaded | A `ConfigError` is raised matching "isn't one specky knows" |
| A prompt is piped to Devin via stdin | Devin runs in print mode | The prompt arrives through `--prompt-file`, since `devin -p` ignores stdin |
