---
type: feature
tags: [ai, configuration, adoption]
---

# Integration — Skill Model Selection

## What It Does
Specky's documentation skills (`find-feature`, `explore-docs`, `document-domain`) can run their lookups and writing on a chosen model instead of whatever model the current session uses. A repo sets `[skills] model` in `specky.toml` to `haiku`, `sonnet`, or `opus`, and the skill hands its work to a subagent on that model. This lets a team spend less on routine doc lookups without switching the whole session.

## How It Works
1. **Read the setting** — a skill reads `[skills] model` from `specky.toml`.
2. **Hand off the work** — the skill passes its task to a subagent configured to run on the chosen model.
3. **Default to the session** — if the setting is unset or set to `inherit`, the skill runs on the session's model as before.
4. **Ask during setup** — `/specky:setup` prompts for the model choice alongside the provider.
5. **Per-host fallback** — because the value names a Claude Code model, other hosts (Codex, opencode, Kiro) get a per-host agent or command under `integrations/` that pins one of their own models.

## Outcomes
| Setting | Result |
| --- | --- |
| `haiku`, `sonnet`, or `opus` | The skill's own work runs on the chosen model via a subagent; the rest of the session is unaffected. |
| Unset | The skill runs on the session's model. |
| `inherit` | The skill runs on the session's model. |
| Non-Claude Code host | Pinned by a per-host agent or command; `[skills] model` alone has no effect. |

## Acceptance Tests
| Given | When | Then |
| --- | --- | --- |
| `[skills] model = "haiku"` in `specky.toml` | A doc skill runs | Its work is handed to a subagent on that model; the session model is unchanged. |
| `[skills] model` unset | A doc skill runs | It runs on the session's model. |
| `[skills] model = "inherit"` | A doc skill runs | It runs on the session's model. |
| A repo being set up in Claude Code | `/specky:setup` runs | It asks which model the skills should run on. |
| A skill pinned to a model in `SKILL.md` frontmatter on a non-Claude Code host | The host configures cheap lookups | A per-host agent or command under `integrations/` pins a host-native model instead. |
| `find-feature` is invoked | The agent answers | It returns the doc path and behaviour ids (`STEP-n`, `OUT-n`, `EDGE-n`, `AT-n`) without reading source. |
