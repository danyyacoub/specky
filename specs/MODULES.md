# specky — Modules

Cross-cutting references: [PRODUCT.md](PRODUCT.md), [GLOSSARY.md](GLOSSARY.md).

The engine's own behavior is documented per feature, in the sections below — Documentation,
Cli, Ai, Docs, Chat, Search, Rendering, Integration — rather than per module in `src/specky/`. The
plugin's skills are documented in place: `setup` (opt a repo in: provider, git hooks, first index)
in [skills/setup/SKILL.md](../skills/setup/SKILL.md), `find-feature` (what a piece of functionality
is meant to do before you use it) in
[skills/find-feature/SKILL.md](../skills/find-feature/SKILL.md), `document-domain` in
[skills/document-domain/SKILL.md](../skills/document-domain/SKILL.md), `explore-docs` (answer
behaviour questions from the docs before reading code) in
[skills/explore-docs/SKILL.md](../skills/explore-docs/SKILL.md), and `launch-viewer` (build and open
the viewer from Claude Code) in [skills/launch-viewer/SKILL.md](../skills/launch-viewer/SKILL.md).
`find-feature`, `document-domain` and `explore-docs` run on the session's model unless
`[skills] model` in `specky.toml` names another (`setup` asks), in which case they hand their work
to a subagent on it. That value names a Claude Code model, so the other hosts pin a tier through a
per-host agent templated under [integrations/](../integrations/).

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
| [cli/document.md](cli/document.md) | Document one named feature or workflow by letting a model search this repo's code for it |

## Ai

How specky talks to the configured AI provider, and what that costs. Cross-cutting: every
command that calls a model goes through this layer.

| Doc | Purpose |
|---|---|
| [ai/provider-cost-controls.md](ai/provider-cost-controls.md) | Prompt prefix caching, batched requests, and per-task model routing |

## Docs

| Doc | Purpose |
|---|---|
| [docs/search-and-indexing.md](docs/search-and-indexing.md) | Index documentation and git history with SQLite FTS5, search via CLI, render searchable static HTML site |

## Chat

| Doc | Purpose |
|---|---|
| [chat/local-rag-server.md](chat/local-rag-server.md) | Local HTTP server that answers documentation questions by retrieving context from FTS5 index |
| [chat/spec-assistant-scoping.md](chat/spec-assistant-scoping.md) | Spec Assistant input autocompletes domain/feature references to narrow retrieval and place drafts |
| [chat/serve-access-control.md](chat/serve-access-control.md) | Serve the viewer and chat from one port, with a configurable origin allowlist and optional token |
| [chat/spec-assistant-conversation.md](chat/spec-assistant-conversation.md) | Spec Assistant keeps session-based conversation history for follow-up questions |
| [chat/spec-assistant-panel.md](chat/spec-assistant-panel.md) | Spec Assistant as a full-height dock: short answers with Read more, draft step cards, intent steering |
| [chat/spec-drafting-workflow.md](chat/spec-drafting-workflow.md) | Draft a spec change in steps — scope, impact, approved acceptance tests, final draft |
| [chat/mcp-host-guidance.md](chat/mcp-host-guidance.md) | MCP server sends connect-time instructions and bundles an explore-docs skill so host models answer behaviour questions from docs before reading code |

## Search

| Doc | Purpose |
|---|---|
| [search/fts5-syntax-safety.md](search/fts5-syntax-safety.md) | Handle FTS5 syntax characters safely in search and chat queries |

## Rendering

| Doc | Purpose |
|---|---|
| [rendering/diagram-support.md](rendering/diagram-support.md) | Server-side mermaid diagram rendering with glossary tooltips and styled tables in the HTML viewer |
| [rendering/html-viewer-shell.md](rendering/html-viewer-shell.md) | Interactive HTML viewer with tag/type filtering and cross-doc navigation |
| [rendering/cross-doc-links.md](rendering/cross-doc-links.md) | Resolve in-body doc links to rendered pages/sections instead of .md repo paths, with heading anchors and link rewriting for viewer, export and chat answers |
| [rendering/home-activity-brief.md](rendering/home-activity-brief.md) | Home page brief of who changed what on the mainline: merged branches as one change, told by their history docs, agents and bots left out |

## Integration

| Doc | Purpose |
|---|---|
| [integration/marketplace-installation.md](integration/marketplace-installation.md) | Install specky from its GitHub marketplace and PyPI, then opt a repo in with `/specky:setup` |
| [integration/skill-model-selection.md](integration/skill-model-selection.md) | Let users pick the model specky's doc lookup and authoring skills run on, per host, via [skills] model or a pinned subagent. |
| [integration/codex-setup.md](integration/codex-setup.md) | Connect specky to Codex manually — MCP server config, skills in .agents/skills, and the optional specky-lookup subagent. |
| [integration/bedrock-provider.md](integration/bedrock-provider.md) | Run specky on Claude via Amazon Bedrock with existing AWS credentials, no Anthropic key |
| [integration/devin-provider.md](integration/devin-provider.md) | Drive Devin CLI as a headless agent provider, including on Devin VMs and fresh clones |

## Catalog

| Doc | Purpose |
|---|---|
| [catalog/feature-graph.md](catalog/feature-graph.md) | List, tag and link classified feature/workflow docs, and emit the feature/workflow graph as Mermaid |
