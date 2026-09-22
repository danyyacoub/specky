# Changelog

Notable changes to specky, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). A release is cut by pushing a `vX.Y.Z` tag whose
version matches `specky.__version__`, `.claude-plugin/plugin.json` and a heading below.

## [Unreleased]

### Added
- **`find-feature` skill.** Before an agent calls into a feature, it asks the docs what that feature
  is *meant* to do and answers with the doc path and its behaviour ids (`STEP-n`, `OUT-n`, `EDGE-n`,
  `AT-n`) — without reading source. `explore-docs` keeps the broader "how does X work?" and "why did
  it change?" questions.
- **Choose the model specky's skills run on.** `[skills] model` in `specky.toml` (`haiku`, `sonnet`,
  `opus`) has `find-feature`, `explore-docs` and `document-domain` hand their work to a subagent on
  that model, so only the skill's own work moves off the session's model. Unset, or `inherit`, they
  run on the session's model. `/specky:setup` asks. The value names a Claude Code model, so Codex,
  opencode and Kiro each get a per-host agent (or command) under `integrations/` that pins one of
  theirs.

## [0.1.0] - 2026-09-22

The first public release. It installs as a Claude Code plugin from GitHub and as a CLI from PyPI.

### Added
- **Docs kept in place.** Functional docs live at `specs/<domain>/<topic>.md`, next to the code,
  with `specs/MODULES.md`, `specs/GLOSSARY.md` and `specs/PRODUCT.md` as shared indexes.
- **Commit-driven docs.** `specky install-git-hook` installs post-commit, post-merge and
  post-rewrite hooks. Each fire documents a slice of the undocumented backlog as
  `specs/history/<sha8>.md` and updates the affected feature and workflow docs in a follow-up
  commit. `specky sync` catches up on history, and `--batch` sends a backfill through the Anthropic
  Message Batches API at half price.
- **`specky document "<feature>"`.** A bounded tool conversation that reads the code for one feature
  and writes its doc. It refuses a doc written without reading any code, and refuses a rewrite that
  drops a section.
- **`specky adopt`.** Moves an existing `docs/` tree into specky's layout, marked as human-authored.
- **Index and search.** `specky index` builds a SQLite FTS5 index of the docs and git history, and
  `specky search` queries it.
- **Viewer.** `specky render-html` builds a static, searchable site that works over `file://`, with
  server-side mermaid diagrams, glossary tooltips, staleness badges and "who to ask" owners.
  `specky serve` adds the Spec Assistant chat, which answers from the docs and drafts spec changes.
- **CI gate.** `specky check` fails a range that changes code without updating the doc that
  describes it. It runs offline and needs no API key. `specky pr-comment` summarises a range's doc
  changes as markdown.
- **More outputs.** `specky tests` scaffolds pytest files from the docs' Acceptance Tests tables,
  and `specky export` writes the docs as one HTML, PDF or Confluence file.
- **Diagnostics.** `specky doctor` checks toolchain, config, hook, index and site health, and
  `specky cost` reports provider calls, cache hits and spend.
- **Providers.** Anthropic, any OpenAI-compatible endpoint, or a local command such as `claude -p`.
  A `<task>_model` setting overrides the model for one kind of call, responses are memoized in the
  index, and prompt prefixes are cached.
- **Claude Code plugin.** An MCP server (`specky-mcp`) for doc search, reading, behaviours and
  history, and four skills: `setup`, `document-domain`, `explore-docs` and `launch-viewer`.
- **Other agents.** Integration guides for opencode, Kiro and Devin.
- **`specky --version`.**

### Changed
- The plugin stays inert in repos that haven't opted in. Its commit hook only runs where
  `specky.toml` exists, and its MCP server tells the host model to leave its tools alone in a repo
  with no docs.
- `specky init` adds `specky.toml` to the repo's `.gitignore`, and `.specky/` ignores itself, so
  setting specky up never leaves untracked files behind.

[Unreleased]: https://github.com/danyyacoub/specky/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/danyyacoub/specky/releases/tag/v0.1.0
