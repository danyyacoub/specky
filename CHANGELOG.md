# Changelog

Notable changes to specky, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). A release is cut by pushing a `vX.Y.Z` tag whose
version matches `specky.__version__`, `.claude-plugin/plugin.json` and a heading below.

## [Unreleased]

### Added
- **`specky install-git-hook --on commit|merge|none`** picks when the doc commits land: after each
  commit (the default, unchanged), once per local merge or pull, or never, leaving it to `specky
  sync` or CI. Every commit is still documented. Re-running switches modes, and `specky doctor` and
  the Claude Code plugin's commit trigger respect the chosen one.
- **`specky lint`** checks the docs as a set, offline, on the worktree:
  - terms several docs use that `GLOSSARY.md` doesn't define (bold terms and Outcomes/Status table
    labels);
  - tags outside the new `TAGS.md` registry, or carried by one doc only when there's no registry;
  - numbers two docs sharing a tag attach to the same name differently.

  `specky check` reports the same findings for the docs a pull request is about. Advice only;
  `--strict` exits 1.
- **Dropped facts are reported.** A doc update that stops stating a number, formula or glossary
  definition now says so:
  - the commit hook's and `specky document`'s line ends `— removed N fact(s): …`;
  - `specky check` lists each doc's removed facts, constants first, however the rewrite happened
    (hook, `document`, an agent's skill, or by hand).

  Reported, never refused: a threshold the code changed drops its old value legitimately.
- **`specky adopt --verify`** reviews a migration that *rewrote* an old docs tree instead of importing
  it. For each old doc it reports every number, formula and defined term that no new doc states,
  looking across the whole tree before calling anything missing. Pairing uses `origin:` first, then
  root GLOSSARY/PRODUCT/MODULES files with their counterparts, then the import mapping. It makes no
  AI call and writes nothing. `--only GLOB` (also for a normal import) replaces the docs/adr
  conventions with one tree.
- **Tag registry.** `TAGS.md` in the docs root lists the tags docs may carry, in a file an agent can
  read without running anything. `specky tags --write` seeds or extends it. With one, the classifier
  and `specky document` are offered only its tags.
- **`## Constants & Invariants` and `## Maintainer Notes`** in the doc templates, in the generator, in
  `specky document` and in the `document-domain` skill:
  - thresholds, weights, formulas and precedence orders are stated verbatim, where "focus on WHAT"
    used to summarise them away;
  - field consumers, lockstep implementations and runbook steps get a home.

  The skills also gain "one owner per rule" (link to the doc that states a rule rather than
  restating it), read tags from `TAGS.md`, and run `specky lint` on what they wrote.
- **Slash commands.** The plugin ships `/specky:doctor`, `/specky:check`, `/specky:lint`,
  `/specky:verify-migration`, `/specky:search`, `/specky:sync` and nine more, one per CLI workflow.
  Each runs its command and explains the result, and asks before anything that costs AI calls,
  moves files or posts. opencode and Codex can copy them in.
- **Tag siblings in the viewer.** Each page's Related block lists the docs sharing a tag with it,
  after its `related:` links. The list is derived at render time, so nobody has to write backlinks.
- **The session agent writes its own docs.** With `provider = "agent"`, specky no longer starts a
  headless copy of the agent from inside that agent's session:
  - A commit made there is left for the new `document-commits` skill. The skill writes the history
    doc and updates the feature doc with the repo already in context, using two new commands:
    `specky pending --json` and `specky record-commit`.
  - `specky document` points at the `document-domain` skill instead. `--headless` restores the old
    behaviour.

  Commits from a terminal or CI, every API provider (Anthropic, Bedrock, OpenAI-compatible,
  `command`), and agents that set no session marker (Kiro, Cursor, Devin) work as before. Set
  `[ai] skill_handoff = false` to opt out.
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

### Changed
- **`specky doctor` fails when docs have diagrams and the mermaid renderer is missing.** Without the
  renderer, the viewer shows diagrams as text and the check that a new diagram parses is off. With no
  diagrams yet it still only warns. The `setup` skill now installs the renderer when Node is present.

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
