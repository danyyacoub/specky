# specky

Coding-agent plugin that generates and maintains functional docs in place
(`specs/<domain>/<topic>.md`), indexes them and git history in SQLite (FTS5), renders a static
searchable HTML viewer, and fails CI when a changed file's doc isn't updated.

First-class Claude Code plugin. Also works with [opencode](integrations/opencode/README.md),
[Kiro](integrations/kiro/README.md), and [Devin](integrations/devin/README.md) via a shared
`SKILL.md` + MCP config — see [integrations/](integrations/).

## Install

```bash
uv tool install --editable /path/to/specky   # once published: uv tool install specky
```

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). Node is optional, only for rendering
`mermaid` diagrams. Install globally, not into a project `.venv` — the git hooks shell out to the
absolute path of the `specky` binary that installed them.

As a Claude Code plugin:

```bash
claude plugin marketplace add /path/to/specky
claude plugin install specky
```

Other agents: wire up the MCP server + `document-domain` skill by hand (config file each) — see
[opencode](integrations/opencode/README.md), [Kiro](integrations/kiro/README.md),
[Devin](integrations/devin/README.md). Everything below is plain CLI, identical everywhere.

## Configure

```bash
specky init               # choose/configure AI provider, writes specky.toml (gitignored)
specky install-git-hook   # post-commit/post-merge/post-rewrite hooks, every future commit documented
```

`init` supports Anthropic, any OpenAI-compatible endpoint, or an arbitrary local command (e.g.
`claude -p`). Non-interactive:

```bash
specky init --yes                          # defaults, no prompts
specky init --yes --no-validate            # ...and skip the live test call
specky init --provider openai-compatible --base-url https://api.deepseek.com \
            --model deepseek-chat --api-key-env DEEPSEEK_API_KEY
```

`--provider` implies `--yes`. `--docs-root NAME` sets `[docs] root` if `specs/` collides with
something else in the repo. `SPECKY_DISABLE_HOOK=1` makes every hook fire a no-op (print + return).

Repo already has docs (`docs/`, `adr/`, `ARCHITECTURE.md`, ...):

```bash
specky adopt --dry-run    # show docs/billing/refunds.md -> specs/billing/refunds.md mapping
specky adopt              # git mv, marked authored: human so the hook won't rewrite them
specky tag && specky index
specky sync --since v1.2.0
```

Details: [doc-adoption.md](specs/documentation/doc-adoption.md).

## Use

```bash
specky bootstrap [PATH] [--max-domains N] [--dry-run] [--yes] [--batch]  # first docs, from the code
specky sync [--dry-run] [--since REV|DATE] [--limit N] [--all-branches] [--no-bootstrap] [--batch]
specky index                    # rebuild .specky/index.db (FTS5) from specs/ + git log
specky search "<query>"         # keyword search
specky render-html               # static site -> .specky/site/index.html
specky serve [--port] [--host]  # viewer + chat ("Ask") endpoint on one port
specky check [--base REV] [--since REV|DATE] [--advisory] [--json]  # CI gate, no AI call
specky pr-comment --base REV    # markdown summary of doc changes, stdout only, never posts
specky tests [--force]          # scaffold tests/spec/test_<domain>_<topic>.py from doc tables
specky export [--pdf|--confluence] [--include-history]  # docs as one handable file
specky doctor [--json]          # toolchain/config/hook/index/site health check
specky cost [--since DATE] [--clear-cache]  # provider calls, cache hits, chars per command+model
```

Each commit hook fire documents up to 5 of the newest 20 undocumented commits (backlog, not
`HEAD`), writing `specs/history/<sha8>.md` and updating the relevant `specs/<domain>/<topic>.md` in
a follow-up commit. Amend/rebase renames the affected history docs instead of duplicating them.
Nothing commits mid-rebase/cherry-pick; those paths queue in `.specky/deferred-docs`.

On a repo with no docs yet, `specky sync` reads the **code** first rather than the commit log: one
call works out what the product is and which domains it has, then one call per domain writes its
`specs/<domain>/<topic>.md`, plus `PRODUCT.md` and `GLOSSARY.md`. Only then does it walk the last 10
commits, which by then are already reflected in what it just wrote — so they only get their
`specs/history/` entry. Cost is O(domains), not O(commits): roughly 2 + N calls, against 2–3 *per
commit* for the `--since <first commit>` backfill that was the only broad-coverage option before.

It is idempotent and resumable with no state on disk: a domain that already has a doc is skipped,
`--max-domains` bounds one run, and re-running picks up the rest. `specky bootstrap` runs the same
pass on demand — after adding a module, or scoped to a subtree (`specky bootstrap src/billing`).
`specky sync --no-bootstrap` opts out. Every subsequent run is the incremental commit-driven path,
unchanged.

`--batch` on either command sends the independent calls — every domain's doc, or every commit's
summary — as one Message Batches request at half the per-token price. It is asynchronous (specky
waits up to an hour, then leaves the batch running and tells you to re-run; whatever landed is
already cached), so it is the right trade for a backfill and the wrong one for anything you're
waiting on. Anthropic provider only; elsewhere it says so and runs normally. Only calls that
depend on nothing but their own input are batched — classification stays serial and in commit
order, because it's fed the running list of docs and answering a whole backlog against one frozen
snapshot is how a run ends up with three docs about one subject.

For a whole domain at once with an agent that can actually read the tree: `/document-domain billing`
runs the bundled [`document-domain`](skills/document-domain/SKILL.md) skill.

Add `owner: <name/team/channel>` to a generated doc's frontmatter to say who to ask about it — shown
in the viewer as "Who to ask", never overwritten by regeneration.

`specky check` fails only when a changed file has a doc describing it that wasn't updated in the
same range, and only after the file/doc pair has recurred across separate commits
(`min_link_commits`). Missing docs and stale docs are reported as advice, never fail the build.
Details: [check.md](specs/cli/check.md).

CI (`.github/workflows/docs.yml`, offline, no API key):

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

Optional CI job to catch commits that bypass the local hook (squash-merges, contributors without
the hook installed): run `specky sync --limit N --yes && specky index`, then open a PR with the
result if `specs/` changed. Needs an API key. Guard it against its own output with a
`[skip specky]` marker in the commit subject.

## Configuration reference

`specky.toml` at repo root, gitignored, written by `specky init`. Only `[ai]` is required.

```toml
[ai]
provider = "anthropic"              # anthropic | openai-compatible | command
model = "claude-haiku-4-5"
api_key_env = "ANTHROPIC_API_KEY"   # env var name, never the key itself
max_tokens = 4096
cache = true                        # memoize responses in the index; see `specky cost`

# Optional: point one kind of call at a different model. Everything else uses `model`.
discovery_model = "claude-sonnet-5"  # bootstrap's one whole-repo call — the hardest question
                                     # specky asks, asked once, and it shapes every doc after it
```

`openai-compatible` also needs `base_url`. `command` needs `command` (e.g. `"claude -p"`), no key —
and can't take per-task models, since it has no model to swap. Tasks: `summary`, `classify`, `doc`,
`discovery`, `glossary`, `tag`, `chat`; an unknown `<task>_model` is a config error, not a no-op.
`specky doctor` prints the routing.

Cache: keyed on model **and** prompt, max 20 MB, oldest evicted first. That is specky's own
memoization — an identical re-run costs nothing. Separately, with the `anthropic` provider the
stable half of each prompt (the classification instructions plus the list of every doc that
already exists) is sent as a cached prefix, which the API bills at a tenth of the input rate after
the first call. That half is resent on every commit and grows with the doc set, so it is the
largest thing specky pays for on a mature repo: ~73% off the classification bill at 60 docs, ~84%
at 300. Below a model's minimum cacheable length nothing is cached and nothing is charged extra, so
small repos are unaffected either way.

```toml
# pyproject.toml — prefer over specky.toml for anything CI must see (specky.toml is gitignored)
[tool.specky.docs]
root = "documentation"      # default: specs/. Must be repo-relative. history/ path isn't configurable.

[tool.specky.check]
ignore = [".github/", "*.lock"]   # default: .github/, .specky/, non-specs markdown, lockfiles, *.txt, *.cfg, *.ini
min_link_commits = 2
stale_after_days = 14             # also drives the viewer's staleness badge/filter
```

```toml
[serve]
host = "127.0.0.1"          # binding wider than loopback exposes this to the network
port = 8420
allow_origins = ["*"]       # or ["https://docs.internal", "null"] ("null" = file:// pages)
token = ""                  # if set, required in X-Specky-Token header
```

Static files are never token-gated (a page can't add headers to its own `<link>`/`<script>` load) —
`allow_origins` is what covers them.

## Architecture

- [`commit_doc.py`](src/specky/commit_doc.py) / [`generator.py`](src/specky/generator.py) — per-commit
  doc generation from real git hooks. `pending_commits()` is the source of truth for what's
  undocumented; hook fire, `sync`, `doctor`, and the CI job are all passes over it.
- [`paths.py`](src/specky/paths.py) — only place `specs` is spelled out; never hardcode the docs root.
- [`lock.py`](src/specky/lock.py) — non-blocking `flock` on `.specky/hook.lock`; a busy lock exits 0.
- [`db.py`](src/specky/db.py) — single SQLite schema. All FTS5 `MATCH` queries go through
  `db.fts_match_query()` ([fts5-syntax-safety.md](specs/search/fts5-syntax-safety.md)).
- [`ai_provider.py`](src/specky/ai_provider.py) — single-method `Provider` protocol; `load_provider_from_toml()` is the only construction path.
- [`html_render.py`](src/specky/html_render.py) — static site, must work over `file://` with no
  build step. Mermaid fences render to SVG server-side at `render-html` time via
  [`vendor/mermaid-render/`](src/specky/vendor/mermaid-render/) (resolved by
  [`mermaid_tool.py`](src/specky/mermaid_tool.py)); without `specky setup-diagrams`, fences stay
  plain text. `specs/GLOSSARY.md` terms get hover tooltips (`link_glossary()`).
- [`answer_render.py`](src/specky/answer_render.py) — chat answers through the same render pipeline;
  `sanitize_fragment()` is the trust boundary for model-authored markup, run before the diagram step.
- **MCP server** (`specky-mcp`) exposes the index to agents — `list_features`, `commits_for_doc`,
  `get_graph`, `list_tags`, `list_workflows`, `commit_info` — so an agent can check what's already
  documented before writing more.

The file→doc map is derived from git history (three git processes), not from `.specky/` state,
since `.specky/` is gitignored and CI needs to compute the same map from a fresh clone.

Full docs, generated by running specky on this repo: [specs/MODULES.md](specs/MODULES.md),
[specs/PRODUCT.md](specs/PRODUCT.md), [specs/GLOSSARY.md](specs/GLOSSARY.md).

## Development

```bash
scripts/test.sh                 # pytest suite, throwaway git repos in tmpdirs
scripts/doctor.sh               # wraps `specky doctor`
scripts/smoke-test.sh           # index -> search -> render-html against this repo
scripts/reindex.sh [query]      # rebuild FTS5 index + search, fast loop for indexer.py/db.py
scripts/mcp-inspector.sh        # MCP Inspector against src/specky/mcp_server.py (needs Node/npx)
uv run specky-mcp               # MCP server over stdio
```

More in [CLAUDE.md](CLAUDE.md).
