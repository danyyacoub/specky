# specky — Product

## What it is
A coding-agent plugin that generates and maintains functional documentation *in place* in a codebase, keeps it searchable alongside commit history, and renders it as a static HTML site non-technical stakeholders can browse.

## Who it's for
- **Engineers**, who trigger the `document-domain` skill from their coding agent (Claude Code, opencode, or Kiro) to write or update a domain's spec as they build it.
- **Product owners / non-technical stakeholders**, who read the generated HTML site instead of the source code to understand what a feature does and why it changed.

## How it fits together
- A shared `SKILL.md` (the "document-domain" skill) drives doc generation, understood by Claude Code, opencode, and Kiro alike.
- Every commit gets a short AI-generated micro-summary, stored alongside the full docs in a local SQLite index.
- `specky index` / `specky search` / `specky render-html` / `specky serve` operate on that index — see [MODULES.md](MODULES.md) for the breakdown.
