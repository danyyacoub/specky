# specky — Glossary

| Term | Definition |
|---|---|
| **Domain** | A module or area of the codebase (e.g. "billing", "auth") that gets its own `specs/<domain>/` folder. |
| **Feature/workflow doc** | A `specs/<domain>/<topic>.md` file describing what a feature or workflow currently does, for a non-technical reader. This is the **reference** — kept in sync automatically (see Feature sync) or written by hand via the `document-domain` skill. |
| **Feature sync** | The automatic counterpart of the `document-domain` skill: on each commit, classifies whether the change affects a documented feature/workflow, and if so, generates or updates that feature's `specs/<domain>/<topic>.md` in place (update, not append — old content is revised, not stacked). Skips commits with no feature-level behavior change (refactors, formatting, config-only). |
| **Micro-doc** | A short AI-generated summary of a single commit ("what changed and why, one paragraph"), written as `specs/history/<sha8>.md` and mirrored into the `micro_docs` sqlite table keyed by commit sha. A supplementary changelog trail alongside the feature docs, not the reference itself. Produced automatically by the post-commit git hook, independent of any agent tool. |
| **AI provider** | The configured backend specky calls to generate docs/micro-docs/chat answers: `anthropic` (default low-effort model), `openai-compatible` (user endpoint), or `command` (user's own local CLI). Configured via `specky.toml`. |
| **Index** | The local SQLite database (`.specky/index.db`) holding `documents`, `commits`, `micro_docs`, and their FTS5 virtual tables. |
| **Viewer** | The static HTML site rendered by `specky render-html`, browsable via `file://` with no server required. |
| **Chat companion** | The optional local process (`specky serve`) that answers questions about the docs inside the HTML viewer, using FTS5 retrieval over the index as RAG context. |
