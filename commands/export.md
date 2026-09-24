---
description: Export the docs as one self-contained HTML page, a PDF, or Confluence storage format.
argument-hint: "[--pdf | --confluence] [--include-history] [--title T]"
allowed-tools: Bash(specky export:*), Bash(uv run --project:*)
---

# specky export

Run `specky export $ARGUMENTS` from the repo root (or
`uv run --project "${CLAUDE_PLUGIN_ROOT}" specky export $ARGUMENTS`). Report where the output
landed and relay any note it prints. `--pdf` needs weasyprint installed, and diagrams need
`specky setup-diagrams`. Don't upload or send the file anywhere unless asked.
