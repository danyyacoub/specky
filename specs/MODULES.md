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

## Cli

| Doc | Purpose |
|---|---|
| [cli/sync.md](cli/sync.md) | Backfill micro-documentation for commits that don't yet have one (idempotent resync) |
