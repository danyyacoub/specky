# specky

Functional documentation that keeps itself current, for repos where nobody has time to write it.

specky is a coding-agent plugin that generates and maintains functional docs **in place** —
`specs/<domain>/<topic>.md`, committed alongside your code — indexes them and your git history in
SQLite, renders a searchable static HTML viewer for people who don't read code, and enforces in CI
that the docs describing a file get updated when that file changes.

Nothing is generated into a wiki you'll forget about, and nothing needs a build step to read: the
viewer is a folder of HTML you can double-click.

Works as a Claude Code plugin (first-class) and, through a shared `SKILL.md` + MCP config, with
opencode and Kiro.

## Install

The git hooks run `specky` from a plain shell, so install it globally rather than only inside a
project's `.venv` (the hooks fall back to the absolute path of the `specky` that installed them, so
a login-less PATH in a GUI git client still works — but a global install is what keeps that path
valid):

```bash
uv tool install --editable /path/to/specky   # once published: uv tool install specky
```

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). Node is optional, needed only for
rendering ```mermaid``` diagrams in the viewer (see [Browse](#browse)).

As a Claude Code plugin:

```bash
claude plugin marketplace add /path/to/specky
claude plugin install specky
```

## Configure

In the repo you want documented:

```bash
specky init               # choose and configure an AI provider, writes specky.toml
specky install-git-hook   # every future commit gets documented
```

`specky init` supports Anthropic, any OpenAI-compatible endpoint (DeepSeek, a local vLLM, …), or an
arbitrary local command — including the `claude` CLI in print mode, if you'd rather not manage a
second API key. It writes `specky.toml`, which is gitignored: it can hold the name of an API-key
env var, never a key.

`install-git-hook` writes three hooks — `post-commit`, `post-merge` and `post-rewrite` — into
wherever git actually runs hooks from (`core.hooksPath` if pre-commit/husky/lefthook set it, and the
shared common dir in a `git worktree`). One hook isn't enough: `post-commit` fires for `git commit`
only. A hook file specky didn't write is never overwritten.

If `specs/` already means something else in your repo — OpenAPI documents, a Rust `specs` crate, an
ECS module — `specky init` notices and offers another name, writing `[docs] root` for you. You can
also set it by hand; put it in `pyproject.toml` for CI to see it (see [`[docs]`](#docs--where-the-docs-live)).

### A repo that already has docs

Don't let the first sync invent twins of docs you already wrote — import them first:

```bash
specky adopt --dry-run    # show the mapping: docs/billing/refunds.md -> specs/billing/refunds.md
specky adopt              # git mv them in, marked `authored: human` so the hook won't rewrite them
specky tag && specky index
specky sync --since v1.2.0   # narrow on purpose: your existing docs already cover what came before
```

`adopt` finds tracked markdown under `docs/`, `doc/`, `documentation/`, `adr/`, `rfc/`, nested
`decisions/`, plus `ARCHITECTURE.md`/`DESIGN.md`/`RUNBOOK.md`/`OPERATIONS.md`; `--include GLOB` and
`--exclude GLOB` adjust that. It makes no AI call, commits nothing, and a destination that's already
taken is reported and skipped rather than silently renamed. Full behaviour:
[specs/documentation/doc-adoption.md](specs/documentation/doc-adoption.md).

## Document

After `install-git-hook`, commit as usual. The hooks are real git hooks, so they fire for any tool,
agent or human. Each fire writes a short summary to `specs/history/<sha8>.md` and
generates/updates the feature-level doc the commit affects (`specs/<domain>/<topic>.md`), skipping
commits with no feature-level behaviour change. Generated docs land in a follow-up commit of their
own, which stages exactly the files that run wrote — an in-progress doc edit of yours is never swept
into it.

A fire works on the **backlog**, not on `HEAD`: it documents up to 5 of the newest 20 undocumented
commits. That's what makes a merge, a rebase, a cherry-pick, a `git am`, a squash-merge in the
GitHub UI or a teammate with no hook installed get documented anyway — a hook only has to fire
eventually. An amend or rebase *renames* the history doc it already paid for instead of orphaning it,
so nothing is generated twice. Nothing is committed while a rebase or cherry-pick is in progress —
those paths are noted in `.specky/` and committed by the next fire instead.

Catch up on anything older than that window, or on a repo the hooks have never run in:

```bash
specky sync --dry-run          # list what it would document, calling no provider
specky sync                    # backfill; stops to confirm at 25 commits or more
specky sync --since v1.2.0 --limit 20
specky sync --all-branches     # commits reachable from any ref, not just HEAD
```

For the commits no developer's machine ever sees — a squash-merge done in the forge's UI, a push from
a contributor without the hook — add a job that reconciles the default branch and opens a PR with
whatever it wrote, as `.github/workflows/docs-sync.yml`:

```yaml
name: docs-sync

on:
  push:
    branches: [main]
  schedule:
    - cron: "17 4 * * *"     # a capped run leaves a backlog nothing else comes back for
  workflow_dispatch:

concurrency:                 # two runs would open two PRs for the same commits
  group: specky-docs-sync
  cancel-in-progress: false

permissions:
  contents: write            # push the docs branch
  pull-requests: write       # open the PR

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0     # sync diffs the whole history; a shallow clone looks undocumented
      - name: Skip our own doc commits
        id: guard
        run: |
          subject=$(git log -1 --format=%s)
          if [ "${subject#docs: sync specky docs \[skip specky\]}" != "$subject" ]; then
            echo "skip=true" >> "$GITHUB_OUTPUT"
          fi
      - uses: astral-sh/setup-uv@v5
        if: steps.guard.outputs.skip != 'true'
      - if: steps.guard.outputs.skip != 'true'
        run: uv tool install specky
      - name: Configure the provider
        if: steps.guard.outputs.skip != 'true'
        run: |
          cat > specky.toml <<'TOML'
          [ai]
          provider = "anthropic"
          model = "claude-haiku-4-5"
          api_key_env = "ANTHROPIC_API_KEY"
          TOML
      - name: Reconcile the doc backlog
        if: steps.guard.outputs.skip != 'true'
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          specky sync --limit 50 --yes   # --limit bounds the spend; the next run picks up the rest
          specky index
      - name: Open a pull request
        if: steps.guard.outputs.skip != 'true'
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          [ -n "$(git status --porcelain -- specs)" ] || exit 0
          branch="specky/docs-sync-${{ github.run_id }}"
          git config user.name "specky[bot]"
          git config user.email "specky[bot]@users.noreply.github.com"
          git checkout -b "$branch"
          git add -- specs        # so the specky.toml written above can't ride along
          git commit -m "docs: sync specky docs [skip specky]"
          git push -u origin "$branch"
          gh pr create --title "docs: sync specky docs" \
            --body "Generated by \`specky sync\` — read it as a draft."
```

A PR rather than a push to `main`: docs written by a model are a change somebody should read, and a
job that can push to your default branch is a much bigger thing to hand a CI runner. This is the one
job that needs an API key — `index` and `check` are offline. The `[skip specky]` guard is what stops
merging the PR from triggering the next run.

Add a line to any generated doc's frontmatter to say who to ask about it:

```yaml
---
type: feature
owner: Payments team          # a name, a team, a Slack channel — whatever a reader can go and ask
---
```

The viewer shows it as a "Who to ask" line, `specky features` prints it, and `specky check` notes
docs in a change that don't have one (advice — it never fails a build). Regeneration preserves it:
nothing generates an owner, so the hook can't overwrite the one you wrote.

For a whole area at once, ask your agent to run the bundled
[`document-domain`](skills/document-domain/SKILL.md) skill (`/document-domain billing`): it reads
the code, then writes the domain's docs directly, which produces better structure than
commit-by-commit generation can.

## Browse

```bash
specky index                    # build/refresh the SQLite FTS5 index from specs/ + git log
specky search "refund flow"     # keyword search from the terminal
specky render-html              # write a static site to .specky/site/index.html
```

Open `.specky/site/index.html` in any browser — sidebar grouped by domain, per-doc pages,
client-side search, no server and no build step. Assets are pulled in with relative
`<link>`/`<script src>` rather than `fetch`, so a double-clicked `file://` page works.

Diagrams are optional and one-time:

```bash
specky setup-diagrams   # installs the Node renderer into ~/.cache/specky (needs Node)
```

With it, ```mermaid``` fences render to static SVG at `render-html` time — no client JS shipped.
Without it, the fenced source stays as plain text and everything else is unaffected.

To turn on the "Ask about these docs" widget, run the companion server:

```bash
specky serve   # viewer + chat on http://127.0.0.1:8420 — Ctrl+C to stop
```

It serves `.specky/site/` too, so `http://127.0.0.1:8420/` is the same viewer with the widget
talking to it same-origin — which is also how you'd share it over a port instead of a file path.
Follow-ups work: the widget keeps one conversation per browser tab, so "why?" and "what about the
other one?" resolve against what was already asked, even after clicking through to another doc.
**New** starts over. The last few turns live in the server's memory and are dropped when it stops —
nothing about a conversation is written to disk.
Served pages get exact full-text search straight from the FTS5 index; the `file://` site falls back
to its in-page index, and the widget tells the reader to start the server rather than failing
silently. See [`[serve]`](#serve--the-chat-server) for who is allowed to talk to it.

Some readers won't open a folder of HTML, and some want the docs where the rest of the company's
already are. `specky export` writes the same rendered content — same markdown pipeline, same tables,
same server-side diagrams — as one file you can hand over:

```bash
specky export                          # .specky/export/specky-docs.html: one page, no JavaScript
specky export --pdf                    # ...and print it, if weasyprint is installed
specky export --confluence             # one storage-format XHTML per doc, plus an index page
specky export --include-history        # add the per-commit notes, normally left out
```

The single page has a table of contents, a print stylesheet and no scripts at all, so it survives
being emailed, opened off a share, or printed from any browser — `--pdf` is a convenience, not the
only route to one. `specs/history/` is excluded by default: it's one doc per commit, and a reader
opening a single file wants the docs, not the changelog. Nothing is ever truncated; the command
reports what it left out and how big the result is.

## Enforce

`specky check` fails when a change edits code that a doc describes without updating that doc. It
calls no AI provider and walks no history — it answers from the diff and the file→doc map `specky
index` derives from git — so it's free to run on every pull request.

```bash
specky check                      # against origin/HEAD in CI, else HEAD~1
specky check --base origin/main
specky check --since "2 weeks ago"
specky check --advisory           # same report, always exit 0 — how you adopt this on a dirty repo
specky check --json               # for annotations
```

It asks for one honest doc update per changed file, not a checklist: any covering doc counts, a
sibling doc in the same domain counts, and a file/doc pair has to recur across separate commits
before it can fail anything. Missing `specs/history/` entries and files with no doc at all are
reported as advice and never fail the build. Full behaviour: [specs/cli/check.md](specs/cli/check.md).

Drop this in as `.github/workflows/docs.yml`:

```yaml
name: docs

on:
  pull_request:
  push:
    branches: [main]

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0        # check resolves a base revision; a shallow clone has none
      - uses: astral-sh/setup-uv@v5
      # Until specky is on PyPI: uv tool install git+https://github.com/danyyacoub/specky
      - run: uv tool install specky
      - run: |
          specky index
          specky check --base "$BASE"
        env:
          BASE: ${{ github.event.pull_request.base.sha || github.event.before || 'HEAD~1' }}
```

No API key is needed in CI: `index` and `check` are both offline. Put the gate's policy in
`pyproject.toml` (see [`[check]`](#check--the-ci-gate)), because `specky.toml` is gitignored and so
doesn't exist there.

Every doc carries an `## Acceptance Tests` table of Given/When/Then rows. Turn those into a test
file to fill in:

```bash
specky tests            # writes tests/spec/test_<domain>_<topic>.py, one test per row
specky tests --force    # regenerate files you haven't edited yet
```

Each row becomes a `@pytest.mark.skip`ped function with the scenario as its docstring and no
assertion — specky knows what the behaviour is, not how to prove it in your code. Drop the skip
marker as you implement each one; existing files are never overwritten without `--force`, so the
work you put in stays. Also offline, and `specs/history/` is left out.

A reviewer can see from the file list that a doc changed. What the product now *does* differently
takes opening it. `specky pr-comment` prints that as markdown — docs added, updated and removed,
each with its one-line purpose from `MODULES.md`, plus the diff of an updated doc's
`## What It Does` section:

```bash
specky pr-comment --base origin/main                                 # read it first
specky pr-comment --base origin/main | gh pr comment --body-file -   # then post it yourself
```

specky never posts anything: the pipe is yours to run. The output is bounded so it always fits in
one comment — per-commit docs under `specs/history/` are counted rather than listed, and a long
range is cut off with a count of what didn't fit.

## Diagnose

```bash
specky doctor          # toolchain, config, hook, diagram renderer, index, site, pending commits
specky doctor --json
```

Prints `[ok]`/`[warn]`/`[fail]` lines and exits non-zero only on a `fail`. It reports whether your
provider's API-key env var is set, never any part of its value. Run it first whenever something
behaves oddly.

```bash
specky cost                     # calls, cache hit rate and character totals per command and model
specky cost --since 2026-09-01
specky cost --clear-cache       # drop the memoized responses, keep the record of what was spent
```

Every provider call goes through a cache keyed on the model plus the prompt, so re-running `specky
sync` over commits it already documented asks nothing new and costs nothing. `specky cost` reports
in characters, not dollars or tokens: specky knows neither your provider's tokenizer nor its price
list, and a made-up figure would be worse than an honest one.

## How it works

- **Two doc-generation paths, one convention.** The git hooks document commit by commit; the
  `document-domain` skill documents an area in one pass. Both write `specs/<domain>/<topic>.md`,
  kebab-case topic, never `README.md`. An auto-commit is marked so the hooks can't recurse on their
  own doc commits.
- **Reconciliation, not events.** "Which commits have no doc" is derived by diffing the git log
  against `specs/history/`, and that diff is what a hook fire, `specky sync`, `specky doctor` and the
  CI job all act on — three tiers of trigger over one mechanism. Git fires hooks for only some of the
  ways a commit can arrive, so anything that depends on catching the event is a permanent gap;
  anything that reconciles only has to run eventually.
- **One SQLite database.** `.specky/index.db` (gitignored) holds the docs, the commit log, their
  FTS5 tables, the file→doc map, and the memoized provider responses `specky cost` reports on. `specky index` is a full rebuild every run — a specs/ tree and
  a git log are cheap to re-walk, and a rebuild can't drift from reality the way an incremental
  sync could.
- **The file→doc map comes from git, not from local state.** Which docs describe which files is
  derived from committed history in three git processes, none proportional to the length of that
  history. That's deliberate: `.specky/` is gitignored, so a fresh CI clone has to be able to
  compute the same map a developer's checkout has, or `specky check` would have nothing to enforce.
- **The viewer is static.** `render-html` writes plain HTML plus shared `assets/`; mermaid is
  rendered server-side into SVG at render time. `specky serve` adds the chat and exact search on
  top of the same files, and is never required to read them.
- **The MCP server** exposes the index to agents (`list_features`, `commits_for_doc`, `get_graph`, …)
  so an agent can ask what's already documented before writing more.

The docs in [specs/](specs/) are specky's own, generated by running specky on this repo — start at
[specs/MODULES.md](specs/MODULES.md), or [specs/PRODUCT.md](specs/PRODUCT.md) for the shape of the
product and [specs/GLOSSARY.md](specs/GLOSSARY.md) for the vocabulary.

## Configuration reference

All of it optional except `[ai]`, which `specky init` writes for you. `specky.toml` sits at the repo
root and is gitignored.

### `[ai]` — the provider

```toml
[ai]
provider = "anthropic"              # anthropic | openai-compatible | command
model = "claude-haiku-4-5"
api_key_env = "ANTHROPIC_API_KEY"   # the env var's *name*; the key itself never goes in this file
max_tokens = 4096                   # raise if generation reports a truncated response
cache = true                        # memoize responses in the index (default); see `specky cost`
```

`provider = "openai-compatible"` also requires `base_url`, `model` and `api_key_env`.
`provider = "command"` requires `command` (e.g. `command = "claude -p"`) and needs no key at all.

The cache lives in the gitignored `.specky/index.db`, holds at most 20 MB of responses (oldest
evicted first), and is keyed on the model — switching models re-asks rather than serving the old
model's answers. `cache = false` turns it off; the usage log `specky cost` reads is written either
way.

### `[docs]` — where the docs live

`specs/` is only the default name for the tree specky maintains. Read from `[docs]` in `specky.toml`,
or from `[tool.specky.docs]` in `pyproject.toml` — prefer the second, because `specky.toml` is
gitignored and CI has to resolve the same tree or `specky check` reads an empty one:

```toml
[tool.specky.docs]
root = "documentation"      # repo-relative; must be inside the repo
```

Everything follows it: generation, `index`, `search`, `check`, the viewer, `export`, `tests` and
`doctor`. `<root>/history/` is not configurable. A root that's absolute or escapes the repo falls
back to `specs`. Changing the root on a repo that already has docs means moving them yourself —
nothing migrates an existing tree.

### `[serve]` — the chat server

Defaults are open, so the widget works from a `file://` page or from a site served on some other
port with no configuration:

```toml
[serve]
host = "127.0.0.1"          # bind address; anything else exposes this to the network
port = 8420
allow_origins = ["*"]       # or e.g. ["https://docs.internal", "null"] ("null" = file:// pages)
token = ""                  # when set, requests must carry it in an X-Specky-Token header
```

With those defaults, any page open in a reader's browser can POST to the port and read answers
derived from your docs — bound to `127.0.0.1` that means software already running on the machine,
and bound wider it means anyone who can reach the port. Narrow `allow_origins`, or set a `token`,
if the docs aren't for everyone who can reach the server. `specky serve` prints a warning when the
bind address isn't loopback. Static files are never token-gated — a page can't add a header to its
own `<link>`/`<script>` loads — so the origin allowlist is what covers them.

### `[check]` — the CI gate

Read from `[check]` in `specky.toml`, or from `[tool.specky.check]` in `pyproject.toml`. Prefer the
second for anything CI should honour, since `specky.toml` is gitignored:

```toml
[tool.specky.check]
ignore = [".github/", "*.lock"]   # replaces the default ignore list wholesale
min_link_commits = 2              # commits a file/doc pair must recur in before it can fail a build
stale_after_days = 14             # how far a doc may lag its code before it's flagged as stale
```

The default ignore list is `.github/`, `.specky/`, markdown outside `specs/`, lockfiles, `*.txt`,
`*.cfg` and `*.ini`.

`stale_after_days` is read by `specky index`, not only by `check`: the same threshold decides the
"N days behind code" badge and the **Stale** filter in the HTML viewer, so the page and the gate
never disagree. Staleness is always advice — it's reported, and it never fails a build.

## Development

```bash
scripts/test.sh                 # the test suite
scripts/doctor.sh               # env/config snapshot
scripts/smoke-test.sh           # index -> search -> render-html against this repo
uv run --project . specky-mcp   # run the MCP server directly over stdio
```

More in [CLAUDE.md](CLAUDE.md).
