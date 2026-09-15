# specky — Modules

Cross-cutting references: [PRODUCT.md](PRODUCT.md), [GLOSSARY.md](GLOSSARY.md).

The engine's own behavior is documented per feature, in the sections below — Documentation,
Cli, Docs, Chat, Search, Rendering — rather than per module in `src/specky/`. The
`document-domain` skill is documented in place, in
[skills/document-domain/SKILL.md](../skills/document-domain/SKILL.md).

## Documentation

| Doc | Purpose |
|---|---|
| [documentation/domain-documentation-workflow.md](documentation/domain-documentation-workflow.md) | Workflow for generating and maintaining functional documentation for code domains |
| [documentation/auto-commit-docs.md](documentation/auto-commit-docs.md) | Reconcile the history-doc backlog on every git hook fire, so a commit is documented however it landed |
| [documentation/doc-adoption.md](documentation/doc-adoption.md) | Import a repo's existing markdown into the docs tree, and configure where that tree lives |
| [documentation/feature-sync.md](documentation/feature-sync.md) | Automatically generate and update feature/workflow documentation on each commit |
| [documentation/feature-classification-and-tags.md](documentation/feature-classification-and-tags.md) | Classify and tag features and workflows for search and discovery |
| [documentation/auto-doc-commit.md](documentation/auto-doc-commit.md) | Automatically commit generated history and feature docs as a follow-up commit instead of leaving them uncommitted |
| [documentation/stale-doc-detection.md](documentation/stale-doc-detection.md) | Detect and flag documentation that lags behind the code it covers, with viewer badges and check warnings |
| [documentation/doc-ownership.md](documentation/doc-ownership.md) | Add optional owner field to docs so readers know who to ask about them |
| [documentation/acceptance-test-scaffolding.md](documentation/acceptance-test-scaffolding.md) | Generate pytest test scaffolds from Given/When/Then acceptance test tables in documentation |

## Cli

| Doc | Purpose |
|---|---|
| [cli/check.md](cli/check.md) | Fail CI when changed code has a doc describing it that the range didn't update |
| [cli/doctor.md](cli/doctor.md) | Diagnose a specky installation — toolchain, config, hook, index, site, doc backlog |
| [cli/sync.md](cli/sync.md) | Backfill micro-documentation for commits that don't yet have one (idempotent resync) |
| [cli/cost.md](cli/cost.md) | Report provider call statistics, cache hit rate, and character usage |
| [cli/pr-comment.md](cli/pr-comment.md) | Generate markdown summaries of documentation changes in a commit range for GitHub PR comments |
| [cli/export.md](cli/export.md) | Export documentation as single-page HTML, PDF, or Confluence storage format for sharing and archival |
| [cli/init.md](cli/init.md) | Configure specky with interactive interview or command-line flags for CI and automation environments |

## Docs

| Doc | Purpose |
|---|---|
| [docs/search-and-indexing.md](docs/search-and-indexing.md) | Index documentation and git history with SQLite FTS5, search via CLI, render searchable static HTML site |

## Chat

| Doc | Purpose |
|---|---|
| [chat/local-rag-server.md](chat/local-rag-server.md) | Local HTTP server that answers documentation questions by retrieving context from FTS5 index |
| [chat/ask-widget-scoping.md](chat/ask-widget-scoping.md) | Chat input autocompletes domain/feature references to narrow AI retrieval and enable HTML responses |
| [chat/serve-access-control.md](chat/serve-access-control.md) | Serve the viewer and chat from one port, with a configurable origin allowlist and optional token |
| [chat/ask-widget-conversation.md](chat/ask-widget-conversation.md) | Ask widget maintains session-based conversation history for follow-up questions |
| [chat/ask-panel-dock.md](chat/ask-panel-dock.md) | Ask panel as full-height dock with answer rendering and intent steering |

## Search

| Doc | Purpose |
|---|---|
| [search/fts5-syntax-safety.md](search/fts5-syntax-safety.md) | Handle FTS5 syntax characters safely in search and chat queries |

## Rendering

| Doc | Purpose |
|---|---|
| [rendering/diagram-support.md](rendering/diagram-support.md) | Server-side mermaid diagram rendering with glossary tooltips and styled tables in the HTML viewer |
| [rendering/html-viewer-shell.md](rendering/html-viewer-shell.md) | Interactive HTML viewer with tag/type filtering and cross-doc navigation |

## Integration

| Doc | Purpose |
|---|---|
| [integration/marketplace-installation.md](integration/marketplace-installation.md) | Enable specky checkout installation via Claude Code plugin marketplace |
