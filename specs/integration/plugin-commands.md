---
type: feature
tags: [configuration, cli]
sources: [commands/doctor.md, commands/check.md, commands/lint.md, commands/verify-migration.md, tests/test_packaging.py]
---

# Integration — Plugin Commands

## What It Does

The Claude Code plugin ships a slash command for each specky CLI workflow a person or an agent runs
by hand. For example, `/specky:doctor`, `/specky:check`, `/specky:lint` and
`/specky:verify-migration`. Each one runs its `specky` command, then explains what came back: the
fix for each `fail`, the doc a violation names, the constants a migration lost. So "check my docs"
is one command instead of knowing which subcommands to chain and how to read their output.

They live in `commands/` at the plugin root, next to the skills. Claude Code discovers them in
every repo that has the plugin, under the same `/specky:` namespace as the skills. opencode and
Codex can copy the same files in as their own commands or prompts.

## How It Works

1. **Run the CLI** — each command runs `specky <subcommand>`. If `specky` isn't on PATH, it falls
   back to the plugin's own copy through `uv run --project "${CLAUDE_PLUGIN_ROOT}" specky`, the same
   fallback the `document-commits` skill uses. Arguments after the command pass through as
   `$ARGUMENTS`.
2. **Pre-approve the read-only ones** — each command's `allowed-tools` names only the Bash commands
   it runs. `doctor`, `check`, `lint`, `search` and the other read-only commands therefore run
   without a permission prompt.
3. **Confirm before cost or change** — `sync` stops after `--dry-run` and asks, since the real run
   costs AI calls. `adopt` stops after `--dry-run`, since the real run moves files. `tags backfill`
   counts the calls first. `pr-comment` prints and asks before posting. `lint` and `check` propose
   edits and ask before making them.
4. **Explain the output** — each command tells the agent what matters in its report, most important
   first. Examples: violations before advice in `check`, missing constants before missing names in
   `verify-migration`.

## Outcomes

| Command | Runs | Asks before |
|---|---|---|
| `/specky:doctor` | `specky doctor` | running any fix |
| `/specky:check [base]` | `specky index && specky check --advisory` | editing docs |
| `/specky:lint [paths]` | `specky lint` | editing the glossary, tags or docs |
| `/specky:verify-migration <glob>` | `specky adopt --verify --only <glob>` | restoring facts |
| `/specky:adopt [flags]` | `specky adopt --dry-run`, then the import | the import (moves files) |
| `/specky:index` | `specky index` | — |
| `/specky:search <query>` | `specky search` | — |
| `/specky:tags [--write \| backfill]` | `specky tags`, `specky tags --write`, `specky tag` | editing `TAGS.md`; `backfill` (AI calls) |
| `/specky:graph` | `specky graph` | — |
| `/specky:sync [flags]` | `specky sync --dry-run`, then the sync | the sync (AI calls) |
| `/specky:pr-comment [base]` | `specky pr-comment` | posting to a pull request |
| `/specky:cost` | `specky cost` | — |
| `/specky:tests` | `specky tests` | — |
| `/specky:export [flags]` | `specky export` | — |
| `/specky:setup-diagrams` | `specky setup-diagrams` (if Node is present) | — |

Some CLI commands have no slash command:
- Workflows a skill already owns: `init` and `install-git-hook` (the `setup` skill), `document`
  (`document-domain`), `render-html` and `serve` (`launch-viewer`).
- The hook's internals: `commit-doc`, `pending`, `record-commit`.
- `features`, `workflows` and `commit-info`, which the MCP tools already answer.

## Edge Cases

| Situation | What happens | Why |
|---|---|---|
| A command would share a skill's name | Prevented by a packaging test | Commands and skills share `/specky:`, so one would hide the other |
| A subcommand is renamed | A packaging test fails on every command that still runs the old name | Otherwise every repo with the plugin gets a command that errors |
| `$ARGUMENTS` in a shell expansion (`${ARGUMENTS:+…}`) | Forbidden by a packaging test | Claude Code substitutes `$ARGUMENTS` as text before the agent reads it, so the expansion becomes a bash syntax error |
| opencode or Codex | The same files are copied in as `specky-<name>` commands or prompts (see their guides) | Both take `$ARGUMENTS` the same way. They ignore `allowed-tools`, which is Claude Code's |
| Kiro or Devin | No slash commands; the agent runs the `specky` command directly | Neither has user-defined commands, and a command is only that command plus how to read its output |

## Acceptance Tests

| Scenario | Given | When | Then |
|---|---|---|---|
| Commands are shipped | The plugin | List `commands/` | `doctor`, `check`, `lint`, `verify-migration` and `search` exist, each with a `description` |
| No clash with skills | The plugin | Compare command and skill names | No overlap |
| Commands run real subcommands | Every command | Collect the `specky <sub>` in its code spans and bash blocks | Each is a subcommand in `specky --help` |
| No shell-expanded arguments | Every command | Search for `${ARGUMENTS` | Absent |
| Costly work waits | `/specky:sync` | Run it | It stops after the dry run and asks |
