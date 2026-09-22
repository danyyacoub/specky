---
name: setup
description: Set specky up in the current repo, so every commit gets documented and the docs become searchable. Checks the specky CLI is installed, chooses and configures an AI provider (specky init), picks the model specky's skills run on, installs the git hooks, and builds the first index. Use when asked to set up, install, configure, enable or get started with specky in a repo, or when specky reports it has no config (specky.toml) here.
---

# Setup
Take a repo from "the specky plugin is installed" to "commits get documented and the docs are
searchable". The plugin does nothing in a repo until this has run: its commit hook and its MCP
instructions both wait for the `specky.toml` that step 3 writes.

Every step is a plain `specky` command or a `specky.toml` edit, so a user can also run them by
hand. Check what's already
done first and skip it. Re-running the skill on a repo that's already set up should change nothing.

## Steps

### 1. Check the tools
Run as **one** Bash call:

```bash
git rev-parse --show-toplevel && command -v uv && uv --version; command -v specky && specky --version
```

- **Not a git repo**: stop. specky documents commits, so it needs one. Say so and don't run
  `git init` yourself.
- **No `uv`**: stop and point the user at https://docs.astral.sh/uv/ to install it. Don't install it
  yourself.
- **No `specky` on PATH**: ask whether to install the CLI with `uv tool install specky`. Run it only
  if they say yes, then repeat the check. Explain why: the git hooks installed in step 5 call a
  global `specky` binary. The plugin's own copy lives in a versioned cache directory that moves on
  every plugin update, so a hook pointing there would break on the next update.

### 2. See what's already set up
```bash
specky doctor --json
```

Each entry has a `section` (`config`, `git hook`, `index`, …), a `status` (`ok`, `warn`, `fail`) and
a `detail`. Skip step 3 if `config` already reports a provider, step 5 if all three `git hook`
entries are `ok`, and step 6 if the index is current. `doctor` doesn't cover step 4: skip it if
`specky.toml` already has a `[skills]` table with a `model` key. If all four are done, say the repo
is already set up and go to the report.

### 3. Choose the AI provider
Ask the user which provider specky should call. Give them these three choices:

1. **Anthropic API** (recommended). This is the only choice where `specky document` gets its full
   tool loop, reading the code before writing a doc. It needs an API key in an environment
   variable. Check the variable is set **without printing it**:
   `[ -n "$ANTHROPIC_API_KEY" ] && echo set || echo missing`. If it's missing, tell the user to
   export it in their shell profile and restart the session. Never ask them to paste a key into the
   chat, and never write one into a file.
   ```bash
   specky init --provider anthropic
   ```
   Add `--api-key-env NAME` if their key lives in a variable with a different name, and `--model`
   if they want something other than the default (`claude-haiku-4-5`).
2. **Claude Code itself (`claude -p`)**. Needs no API key: it runs on the user's Claude
   subscription. The trade-off is that `specky document` degrades to a single call without code
   access. Commit docs and the viewer work the same.
   ```bash
   specky init --provider command --command "claude -p"
   ```
3. **Any OpenAI-compatible endpoint** (DeepSeek, OpenRouter, a local server, …). Ask for the base
   URL, the model name, and the *name* of the env var holding the key, then:
   ```bash
   specky init --provider openai-compatible --base-url <url> --model <model> --api-key-env <VAR>
   ```

`init` makes one small test call to prove the provider works (add `--no-validate` to skip it). It
writes `specky.toml` at the repo root and adds that file to `.gitignore`: the file only names an env
var and holds no secret, but it's per-machine. If `init` says `specs/` already holds files specky
didn't write, relay that and ask whether to re-run with `--docs-root <name>`.

### 4. Choose the model specky's skills run on
`find-feature`, `explore-docs` and `document-domain` run on the session's model unless `specky.toml`
names another. Ask the user which they want:

1. **The session's model** (the default). The skills run on whatever model the conversation uses.
2. **A named model** — `haiku`, `sonnet` or `opus`. Each skill hands its work to a subagent on that
   model and relays the answer, so only the skill's own work moves and the conversation stays on
   the session's model. `haiku` makes lookups cheap; `document-domain` writes whole docs, so their
   quality follows the model picked here.

Append the answer to `specky.toml`, writing `inherit` for the first choice so a re-run knows it was
asked:

```toml
[skills]
model = "haiku"
```

A missing file, table or key, or a name the host can't start a subagent on, runs the skills on the
session's model.

### 5. Install the git hooks
Before running this, tell the user what it does. After every commit, pull and rebase, specky sends
the new commits' messages and diffs to the provider from step 3. It writes `specs/history/<sha>.md`
and updates the affected feature docs in a follow-up commit (`docs: sync specky docs [skip
specky]`). It documents up to 5 commits per fire, so a commit takes a few seconds longer. Ask for a
go-ahead, then:

```bash
specky install-git-hook
```

If it refuses because a hook it didn't write is already there (husky, lefthook, pre-commit), don't
overwrite anything. Relay the line from the error that says what to add to the existing hook by
hand.

### 6. Build the index
```bash
specky index
```

No AI call. This makes the docs and git history searchable by the MCP tools and the viewer.

## Report

- What was already set up and what this run changed: provider, the skills' model, hooks, index.
  Mention that `specky.toml` went into `.gitignore` if `init` said so.
- Next steps, by what the user wants:
  - **Document a feature now**: `specky document "<feature or workflow>"`, or the
    `/specky:document-domain` skill to have you do it with the repo in context.
  - **Browse the docs**: the `/specky:launch-viewer` skill.
  - **Document existing history**: `specky sync --dry-run` lists the undocumented commits and
    estimates the number of AI calls (2–3 per commit). For a large backlog, suggest
    `--since <tag or date>` or `--limit N`, and `specky cost` afterwards to see what it spent.
  - **CI**: the offline `specky check` job in specky's README fails a PR that changes code without
    updating the doc describing it. It needs no API key.
- To pause the hooks without uninstalling them, set `SPECKY_DISABLE_HOOK=1`.
