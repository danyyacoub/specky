---
name: setup
description: Set specky up in the current repo, so every commit gets documented and the docs become searchable. Checks the specky CLI is installed, chooses and configures an AI provider (specky init), picks the model specky's skills run on, installs the git hooks, and builds the first index. Use when asked to set up, install, configure, enable or get started with specky in a repo, or when specky reports it has no config (specky.toml) here.
---

# Setup
Take a repo from "specky is available to this agent" to "commits get documented and the docs are
searchable". specky does nothing in a repo until this has run: its commit hook and its MCP
instructions both wait for the `specky.toml` that step 3 writes.

Every step is a plain `specky` command or a `specky.toml` edit, so it works the same from Claude
Code, Codex, opencode, Kiro or Devin, and a user can also run the steps by hand. Check what's
already done first and skip it. Re-running the skill on a repo that's already set up should change
nothing.

**On Devin's cloud agent**, read [Devin](#devin) first: its VM is rebuilt from a blueprint, so
anything set up by hand in a session is gone in the next one.

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
  global `specky` binary. A Claude Code plugin's own copy lives in a versioned cache directory that
  moves on every plugin update, so a hook pointing there would break on the next update.

### 2. See what's already set up
```bash
specky doctor --json
```

Each entry has a `section` (`config`, `git hook`, `index`, …), a `status` (`ok`, `warn`, `fail`) and
a `detail`. Skip step 3 if `config` already reports a provider, step 5 if all three `git hook`
entries are `ok`, step 6 if `diagrams` is `ok`, and step 7 if the index is current. `doctor` doesn't
cover step 4: skip it if `specky.toml` already has a `[skills]` table with a `model` key. If all
five are done, say the repo is already set up and go to the report.

### 3. Choose the AI provider
Ask the user which provider specky should call. Give them these three choices:

1. **The coding agent they're using** (recommended). Needs no API key.
   - **Inside the agent's session:** specky doesn't launch anything. Commits made there are left for
     the agent's `document-commits` skill, and `specky document` points at the `document-domain`
     skill, so the agent writes the docs itself with the repo already in context.
   - **Outside a session** (a terminal commit, CI): it runs the agent headless on their existing
     login (`claude -p`, `codex exec`, `gemini -p`, `opencode run`, …). There, `specky document`
     falls back to a single call without specky's code-reading tools.

   Ask whether they want a specific model; leave it out to keep the agent's own default.
   ```bash
   specky init --provider agent                 # the agent's own default model
   specky init --provider agent --model opus    # or pin one
   ```
   `init` detects the current agent; add `--agent NAME` to pick another installed one.
2. **Any OpenAI-compatible endpoint** (DeepSeek, OpenRouter, a local server, …). Ask for the base
   URL, the model name, and the *name* of the env var holding the key. Check the variable is set
   **without printing it**: `[ -n "$VAR" ] && echo set || echo missing`. Never ask them to paste a
   key into the chat, and never write one into a file. Then:
   ```bash
   specky init --provider openai-compatible --base-url <url> --model <model> --api-key-env <VAR>
   ```
3. **Claude on Amazon Bedrock**, for a team whose AWS account already has Bedrock model access. No
   Anthropic key: credentials come from the AWS config, profile or role. It needs the AWS SDK extra,
   so install the CLI with `uv tool install 'specky[bedrock]' --force` first. Ask for the model (a
   Bedrock ID such as `anthropic.claude-sonnet-5`; blank keeps `anthropic.claude-haiku-4-5`), and a
   region or profile only if their AWS config doesn't already set one:
   ```bash
   specky init --provider bedrock --model <bedrock model id> --aws-region <region>
   ```

`init` makes one small test call to prove the provider works (add `--no-validate` to skip it). It
writes `specky.toml` at the repo root and adds that file to `.gitignore`: the file only names an env
var and holds no secret, but it's per-machine. If `init` says `specs/` already holds files specky
didn't write, relay that and ask whether to re-run with `--docs-root <name>`.

### 4. Choose the model specky's skills run on
**Claude Code only** — skip this step on any other agent. `[skills] model` names a Claude Code
subagent model, and other hosts pin a skill's model their own way, if at all (each guide under
`integrations/` in the specky repo says how).

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

### 6. Install the diagram renderer
Workflow docs always carry a ```mermaid``` diagram, and the viewer draws them with a small Node
tool. Without it they show as source text, and specky's check that a newly written diagram actually
parses has nothing to parse with — `specky doctor` fails once the docs have a diagram and this is
missing. Skip this step if `doctor` already reports `diagrams` as `ok`.

```bash
command -v node >/dev/null && specky setup-diagrams || echo "no node"
```

No Node: don't install it yourself. Say that diagrams stay as text until Node is installed and
`specky setup-diagrams` is re-run.

### 7. Build the index
```bash
specky index
```

No AI call. This makes the docs and git history searchable by the MCP tools and the viewer.

## Report

- What was already set up and what this run changed: provider, the skills' model, hooks, diagram
  renderer, index.
  Mention that `specky.toml` went into `.gitignore` if `init` said so.
- Next steps, by what the user wants:
  - **Document a feature now**: `specky document "<feature or workflow>"`, or the
    `document-domain` skill to have you do it with the repo in context.
  - **Browse the docs**: `specky render-html`, or `specky serve` for the viewer with its chat.
  - **Document existing history**: `specky sync --dry-run` lists the undocumented commits and
    estimates the number of AI calls (2–3 per commit). For a large backlog, suggest
    `--since <tag or date>` or `--limit N`, and `specky cost` afterwards to see what it spent.
  - **CI**: the offline `specky check` job in specky's README fails a PR that changes code without
    updating the doc describing it. It needs no API key.
- To pause the hooks without uninstalling them, set `SPECKY_DISABLE_HOOK=1`.

## Devin
Devin's cloud agent works in a throwaway VM built from `.devin/blueprint.yaml`, so the setup that
lasts is the blueprint, not this session. Don't run steps 3–7 for their own sake. Instead:

1. Ask which mode the user wants. **Read-only** (the default): Devin searches and hand-edits
   `specs/`, and `specky check` on the pull request is the gate. It needs no provider and no key.
   **Write**: the git hook runs in the VM and documents Devin's commits, which needs a provider
   credential stored as a Devin secret.
2. Propose the blueprint from the specky repo's `integrations/devin/blueprint.yaml`, merged into
   the repo's own if there is one. It installs specky and runs `specky index` on every session.
   For write mode, the `maintenance` step also runs:
   ```bash
   specky init --provider openai-compatible --base-url https://api.anthropic.com/v1 \
     --model claude-haiku-4-5 --api-key-env ANTHROPIC_API_KEY --no-validate
   specky install-git-hook
   ```
   Any OpenAI-compatible endpoint works; the key's variable must match the secret's name. To have
   Devin itself write the docs instead, use `specky init --provider agent --agent devin` and install
   Devin CLI in the blueprint (`curl -fsSL https://cli.devin.ai/install.sh | bash`), restoring its
   `credentials.toml` from a secret: `integrations/devin/README.md` has the steps.
3. Propose `.devin/mcp_config.json` and the `AGENTS.md` paragraph from the same directory.
4. Say that syncing the blueprint is done in Devin's UI (Settings → the repo → Environment) and
   that it applies from the next session.

Running `specky index` in the current session is still worth it, so this session can search the
docs right away.
