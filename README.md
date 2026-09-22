# specky

Functional docs that keep up with your code.

Most code is now written by AI. The people who own it still need a clear view of what it does:
which features exist, how each workflow runs, and how the edge cases are handled. Reading the code
is no longer a practical way to get that view.

specky keeps a functional doc of your codebase in sync with the code. It writes plain-markdown docs
into your repo, updates them on every commit, and indexes them for three audiences:

- **AI agents** look up what a feature does and which rules it must keep before they change it.
- **Developers** review behaviour and edge cases in the PR, next to the code that changed.
- **Product managers** browse a searchable site of features and recent changes with no code to
  read, and use an agent to explore them or draft proposals.

Because every feature is indexed, finding one is a search, not a crawl through the code. That
work can run on a lower-cost model, which keeps your smartest model on the hard development tasks.

It ships as a Claude Code plugin plus a CLI. It also works with [opencode][opencode], [Kiro][kiro]
and [Devin][devin].

## Features

### Docs that follow your commits

A git hook documents each commit. It writes a short history note, updates the feature doc the
commit touched, and commits both as a follow-up. Amends and rebases are handled.

![A commit and the doc update specky made for it][shot-commits]

### Any feature documented on demand

`specky document "the refund flow"` has a model search and read the code for that one feature. It
then writes `specs/billing/refund-flow.md`. Running it again updates the doc in place.

![specky document writing a feature doc][shot-document]

### A searchable docs site

`specky render-html` builds a static site that non-engineers can browse, with no server or build
step. It has full-text search over docs and history, tag filters, diagrams, glossary tooltips and
stale-doc badges. Its home page shows recent changes.

![The docs site][shot-site]

### Spec Assistant

`specky serve` adds a chat panel to the site. It answers from the docs and cites them. It can also
draft a spec change step by step: scope, impact, acceptance tests, then the final text.

![The Spec Assistant answering a question][shot-assistant]

### A CI gate against doc drift

`specky check` fails a PR that changes code without updating the doc that describes it. It runs
offline and needs no API key.

![specky check failing a branch that skipped its doc update][shot-check]

### Docs your agent reads first

An MCP server and skills let Claude Code, and other agents, answer "what does X do?" from the docs
before reading code.

![Claude Code answering from specky's docs][shot-agent]

### Lower-cost models for doc work

Looking up and writing docs doesn't need your strongest model. In Claude Code, set
`[skills] model = "haiku"` in `specky.toml`. specky's skills then hand their lookups and writing to
a subagent on that model, and your session stays on the model you chose for development. For the
CLI and the git hooks, `[ai] <task>_model` sends each kind of call (commit summaries,
classification, doc writing, tags, chat) to its own model.

specky can also import existing docs (`specky adopt`), export them to PDF or Confluence, summarise
a PR's doc changes, and scaffold tests from a doc's acceptance-test table.

## How it works

1. **Docs live in your repo as markdown**: `specs/<domain>/<topic>.md` for features and workflows,
   and `specs/history/<sha>.md` for commits. You review and version them like code.
2. **Writing calls your AI provider.** Choose Anthropic, any OpenAI-compatible endpoint, or a
   local command such as `claude -p`. Only these commands call it: the commit hooks, `sync`,
   `document`, `tag` and the Spec Assistant. They send the diff, code or docs they are working on.
   Keys stay in environment variables.
3. **Reading is offline.** `specky index` builds a SQLite full-text index of the docs and git log
   in `.specky/`, which is gitignored. Search, the site, `check` and the MCP server all read it.

## Install

You need git, Python 3.11+ and [uv][uv], on macOS or Linux. On Windows, use WSL.

```bash
uv tool install specky                            # the CLI
claude plugin marketplace add danyyacoub/specky   # the Claude Code plugin
claude plugin install specky@specky
```

## Set up

In the repo you want documented, run `/specky:setup` in Claude Code. It picks a provider and the
model its skills run on, installs the git hooks and builds the first index. To do the same from a
plain terminal:

```bash
specky init               # choose a provider; writes specky.toml (gitignored)
specky install-git-hook   # document every commit from now on
specky index
```

Next, document a first feature with `specky document "<feature>"`, then open the site with
`/specky:launch-viewer`. If the repo already has docs, preview importing them with
`specky adopt --dry-run`.

The plugin does nothing in a repo until it has a `specky.toml`.

### CI check

Save this as `.github/workflows/docs.yml`:

```yaml
on:
  pull_request:
  push:
    branches: [main]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: astral-sh/setup-uv@v5
      - run: uv tool install specky
      - run: specky index && specky check --base "$BASE"
        env:
          BASE: ${{ github.event.pull_request.base.sha || github.event.before || 'HEAD~1' }}
```

### Update and uninstall

```bash
uv tool upgrade specky
claude plugin update specky@specky
```

To pause the hooks, set `SPECKY_DISABLE_HOOK=1`. To remove them, delete the `post-commit`,
`post-merge` and `post-rewrite` hooks that call `specky commit-doc`. Then run
`claude plugin uninstall specky@specky` and `uv tool uninstall specky`.

## Learn more

- `specky --help` lists every command and flag.
- [The full docs][modules] cover every feature and setting. specky wrote them from its own code.
- [CHANGELOG][changelog]
- Contributing: from a checkout, run `uv tool install --editable .`, then
  `claude plugin marketplace add "$PWD"` and `claude plugin install specky@specky`. The plugin then
  loads straight from your checkout. Tests and scripts are in [AGENTS.md][agents-md].

MIT licensed.

[opencode]: https://github.com/danyyacoub/specky/tree/main/integrations/opencode
[kiro]: https://github.com/danyyacoub/specky/tree/main/integrations/kiro
[devin]: https://github.com/danyyacoub/specky/tree/main/integrations/devin
[uv]: https://docs.astral.sh/uv/
[modules]: https://github.com/danyyacoub/specky/blob/main/specs/MODULES.md
[changelog]: https://github.com/danyyacoub/specky/blob/main/CHANGELOG.md
[agents-md]: https://github.com/danyyacoub/specky/blob/main/AGENTS.md
[shot-commits]: https://raw.githubusercontent.com/danyyacoub/specky/main/assets/screenshots/commit-docs.png
[shot-document]: https://raw.githubusercontent.com/danyyacoub/specky/main/assets/screenshots/document.png
[shot-site]: https://raw.githubusercontent.com/danyyacoub/specky/main/assets/screenshots/site.png
[shot-assistant]: https://raw.githubusercontent.com/danyyacoub/specky/main/assets/screenshots/assistant.png
[shot-check]: https://raw.githubusercontent.com/danyyacoub/specky/main/assets/screenshots/check.png
[shot-agent]: https://raw.githubusercontent.com/danyyacoub/specky/main/assets/screenshots/agent.png
