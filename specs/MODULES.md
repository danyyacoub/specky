# specky — Modules

Cross-cutting references: [PRODUCT.md](PRODUCT.md), [GLOSSARY.md](GLOSSARY.md).

## Core engine (`src/specky/`)

| Doc | Purpose |
|---|---|
| _(none yet)_ | Indexing, AI provider, and rendering behavior will be documented here as each phase lands. |

## Skills (`skills/`)

| Doc | Purpose |
|---|---|
| _(none yet)_ | The `document-domain` skill itself is documented in [skills/document-domain/SKILL.md](../skills/document-domain/SKILL.md). |

## Documentation

| Doc | Purpose |
|---|---|
| [documentation/domain-documentation-workflow.md](documentation/domain-documentation-workflow.md) | Workflow for generating and maintaining functional documentation for code domains |
| [documentation/auto-commit-docs.md](documentation/auto-commit-docs.md) | Automatically generate AI summaries of commits via git post-commit hook |
| [documentation/feature-sync.md](documentation/feature-sync.md) | Automatically generate and update feature/workflow documentation on each commit |
| [documentation/feature-classification-and-tags.md](documentation/feature-classification-and-tags.md) | Classify and tag features and workflows for search and discovery |
| [documentation/auto-doc-commit.md](documentation/auto-doc-commit.md) | Automatically commit generated history and feature docs as a follow-up commit instead of leaving them uncommitted |

## Cli

| Doc | Purpose |
|---|---|
| [cli/sync.md](cli/sync.md) | Backfill micro-documentation for commits that don't yet have one (idempotent resync) |

## Docs

| Doc | Purpose |
|---|---|
| [docs/search-and-indexing.md](docs/search-and-indexing.md) | Index documentation and git history with SQLite FTS5, search via CLI, render searchable static HTML site |

## Chat

| Doc | Purpose |
|---|---|
| [chat/local-rag-server.md](chat/local-rag-server.md) | Local HTTP server that answers documentation questions by retrieving context from FTS5 index |

## Search

| Doc | Purpose |
|---|---|
| [search/fts5-syntax-safety.md](search/fts5-syntax-safety.md) | Handle FTS5 syntax characters safely in search and chat queries |

## Rendering

| Doc | Purpose |
|---|---|
| [rendering/diagram-support.md](rendering/diagram-support.md) | Server-side mermaid diagram rendering with glossary tooltips and styled tables in the HTML viewer |
| [rendering/html-viewer-shell.md](rendering/html-viewer-shell.md) | Interactive HTML viewer with tag/type filtering and cross-doc navigation |
