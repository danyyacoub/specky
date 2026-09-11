# specky

A coding-agent plugin that generates and maintains functional documentation *in place*, indexes it and commit history in SQLite, and renders a searchable HTML viewer with an AI chat for non-technical stakeholders.

Works as a Claude Code plugin (first-class) and via a shared `SKILL.md` + MCP config with opencode and Kiro.

See [specs/PRODUCT.md](specs/PRODUCT.md), [specs/MODULES.md](specs/MODULES.md), and [specs/GLOSSARY.md](specs/GLOSSARY.md) for what this plugin is; see the plan for the phased build order.

## Status

Phase 1 (scaffold) — plugin manifest, `document-domain` skill, and a placeholder MCP server (`ping`) are in place. AI provider setup, indexing, the HTML viewer, and chat land in Phases 2–4.

## Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)

## Local install (Claude Code)

```bash
claude plugin marketplace add /path/to/specky
claude plugin install specky
```

## Development

```bash
uv run --project . specky-mcp   # run the MCP server directly, for local testing
```
