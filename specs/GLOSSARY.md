# specky — Glossary

| Term | Definition |
|---|---|
| **Domain** | A module or area of the codebase (e.g. "billing", "auth") that gets its own `specs/<domain>/` folder. |
| **Functional doc** | A `specs/<domain>/<topic>.md` file describing what a feature does and why, for a non-technical reader. Produced by the `document-domain` skill. |
| **Micro-doc** | A short AI-generated summary of a single commit ("what changed and why, one paragraph"), stored keyed by commit sha. Produced automatically by the post-commit git hook, independent of any agent tool. |
| **AI provider** | The configured backend specky calls to generate docs/micro-docs/chat answers: `anthropic` (default low-effort model), `openai-compatible` (user endpoint), or `command` (user's own local CLI). Configured via `specky.toml`. |
| **Index** | The local SQLite database (`.specky/index.db`) holding `documents`, `commits`, `micro_docs`, and their FTS5 virtual tables. |
| **Viewer** | The static HTML site rendered by `specky render-html`, browsable via `file://` with no server required. |
| **Chat companion** | The optional local process (`specky serve`) that answers questions about the docs inside the HTML viewer, using FTS5 retrieval over the index as RAG context. |
