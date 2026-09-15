# Devin integration

specky needs two things from any agent: the MCP server (read-only queries over the index) and the
`document-domain` skill. Everything else — the git hook, `specky index`, `render-html`, `serve` — is
plain CLI and identical everywhere.

Devin is the one host so far that isn't a plugin host. There's no directory you drop a plugin into
and no `SKILL.md` path it scans, so the two things above land on four different Devin surfaces:

| What specky needs | Where it goes in Devin | Committed to the repo? |
|---|---|---|
| The `specky` CLI on PATH, and an index to answer from | [`.devin/blueprint.yaml`](blueprint.yaml) | yes |
| The MCP server | [`.devin/mcp_config.json`](mcp_config.json) | yes |
| "docs live in `specs/`, and here's what to run" | `AGENTS.md` ([snippet](agents-md-snippet.md)) | yes |
| The `document-domain` procedure | a playbook ([body](playbook-document-domain.md)) | no — Devin's UI |

The files in this directory are the copies to take. Nothing here reads them at runtime; they're
templates for the repo you want documented.

## Which mode

Devin runs in a throwaway VM and opens a pull request. That makes the choice about *where* docs get
written, and the two answers want different setups:

- **Read-only (start here).** Devin queries the index over MCP, reads and hand-edits `specs/` as
  part of whatever change it's making, and `specky check` on the pull request fails if it changed
  code covered by a doc it didn't touch. No provider key in the VM, no AI spend, no doc commit
  appearing in the branch unasked. Devin's docs go through the same review its code does.
- **Write.** The git hook is installed in the VM and a provider key is a Devin secret, so Devin's
  commits get documented exactly like a developer's. Cheaper in reviewer attention, more surprising:
  a `docs: sync specky docs [skip specky]` commit lands in the branch that Devin didn't mention and
  the reviewer didn't ask for.

Read-only is what the rest of this file sets up. [Write mode](#write-mode) is the delta.

## Blueprint

Devin's environment config is a [blueprint](https://docs.devin.ai/onboard-devin/environment/blueprint-reference):
`initialize` runs when the snapshot is built, `maintenance` on top of each session's clone, and
`knowledge` is reference text handed to the agent rather than executed. Copy
[`blueprint.yaml`](blueprint.yaml) to `.devin/blueprint.yaml` in the repo, merging it into one you
already have, then sync it from the Devin UI (Settings → the repo → Environment) or the API.

Two lines in there matter more than they look:

- `git fetch --unshallow`. Devin's clone can be shallow, and a shallow clone is the repo state that
  makes specky lie rather than fail: `pending_commits()` walks `git log`, so commits below the clone
  depth aren't undocumented, they're *absent* — `specky sync` finds nothing to backfill and
  `specky index` records a fraction of the history. `specky doctor` warns about this now; the fetch
  is what stops it mattering.
- `SPECKY_DISABLE_HOOK=1`. Belt-and-braces for read-only mode: if the repo commits its own hooks and
  points `core.hooksPath` at them (the husky-shaped setup), every clone gets specky's hooks whether
  the VM wants them or not. With the variable set, each fire prints one line and returns.

## MCP server

Copy [`mcp_config.json`](mcp_config.json) to `.devin/mcp_config.json` — project scope, shared with
the team. (`~/.config/devin/mcp_config.json` is the per-user equivalent, and
`.devin/mcp_config.local.json` overrides either without being committed.)

`specky-mcp` is the console script installed by `uv tool install specky` — the same entry point
Claude Code's [.mcp.json](../../.mcp.json) points at. It speaks stdio and answers from
`.specky/index.db`, which is why the blueprint's `maintenance` step runs `specky index`: without
that file every tool returns nothing, and Devin has no way to tell "no docs match" from "no index".

Tools exposed: `ping`, `list_features`, `list_workflows`, `list_tags`, `get_graph`, `commit_info`,
`commits_for_doc`. All read-only.

## AGENTS.md

Devin reads `AGENTS.md` from the repo root before it starts coding, and it's the only one of these
four surfaces that tells Devin what to *do* with any of the rest. Append
[`agents-md-snippet.md`](agents-md-snippet.md) to the repo's `AGENTS.md` (create it if there isn't
one).

The part that earns its space is `specky check --base origin/main`, run before Devin opens the pull
request. It's offline and free, so it costs a session nothing — and it's the difference between
Devin learning that it left a doc stale while it can still fix it, and a red X arriving after the
branch is pushed.

## The document-domain playbook

Devin's equivalent of a skill is a [playbook](https://docs.devin.ai/product-guides/creating-playbooks),
and playbooks live in Devin's UI, not in the repo. So rather than pasting all 152 lines of
`SKILL.md` into a text box where it will drift from the file, vendor the file and point the playbook
at it:

```bash
mkdir -p .devin
cp /path/to/specky/skills/document-domain/SKILL.md .devin/document-domain.md
```

Then create a playbook (Settings → Resources → Playbooks) with
[`playbook-document-domain.md`](playbook-document-domain.md) as its body. It's short on purpose:
it names the vendored path and gets out of the way, so upgrading specky is a `cp` rather than a
re-paste.

`SKILL.md` isn't shipped in the wheel — `uv tool install specky` installs `src/specky` and nothing
else — so the copy has to come from a specky checkout, exactly as in the
[Kiro integration](../kiro/README.md).

## The CI gate is the part that actually holds

Everything above helps Devin write the right docs. None of it *makes* it, and a cloud agent's
session is the least supervised place specky runs. So put the gate where it can't be skipped —
the `specky check` job from the [README](../../README.md#enforce), on pull requests. Devin reads
failing checks on its own PRs and fixes them, which turns the gate into the feedback loop the
AGENTS.md line is only asking for politely.

`specky check` needs no API key. The same is true of `specky index`, `specky pr-comment` and
`specky tests` — the whole read side is offline, which is why read-only mode needs no secret at all.

## Write mode

Only the delta from above.

1. Add the provider key as a Devin secret (Settings → Secrets), named however `specky.toml` says —
   `ANTHROPIC_API_KEY` by default. The blueprint references it as `$ANTHROPIC_API_KEY`; specky reads
   it from the environment and never sees a value from the config file.
2. Configure specky and install the hook in `maintenance`, and drop the `SPECKY_DISABLE_HOOK` line
   from `initialize`:

   ```yaml
   maintenance:
     - name: "Set specky up"
       run: |
         git fetch --unshallow 2>/dev/null || true
         specky init --yes --no-validate
         specky install-git-hook
         specky index
   ```

   `--yes` is what makes this possible: `specky init` is an interview, and a blueprint step has no
   terminal — without it, `input()` raises `EOFError` a question or two in, after some answers have
   been given and before anything is written. `--no-validate` skips the live provider call, which a
   snapshot build shouldn't pay for. Pass `--provider`/`--model`/`--api-key-env` to write something
   other than the default Anthropic config.
3. Expect doc commits in Devin's branches, and tell your reviewers. They carry the
   `docs: sync specky docs [skip specky]` subject, so the hook, the docs-sync workflow and
   `specky check` all recognise them and leave them alone.

Read-only mode plus the CI gate gets you the same docs one merge later, with a human in the loop.
Write mode is worth it when Devin's sessions are the majority of the repo's commits — at which
point the backlog the nightly `docs-sync` job would otherwise carry is the argument.

## Checking it worked

Inside a Devin session:

```bash
specky doctor      # toolchain, config, hook, index, shallow clone, backlog
specky search "<something you know is documented>"
```

`doctor` is the one to read first: it exits 1 only on a `fail`, and the states this integration
tends to land in — no index yet, a shallow clone, the hook disabled on purpose — are all `warn`
rows that name their own fix.
