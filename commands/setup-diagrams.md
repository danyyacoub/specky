---
description: Install the Node tool that renders ```mermaid``` diagrams in the docs viewer and checks new diagrams parse.
argument-hint: "[--force]"
allowed-tools: Bash(specky setup-diagrams:*), Bash(uv run --project:*), Bash(command -v node:*)
---

# specky setup-diagrams

Check `command -v node` first. With no Node, stop: say diagrams stay as plain text until Node is
installed, and don't install Node yourself.

Otherwise run `specky setup-diagrams $ARGUMENTS` from the repo root (or
`uv run --project "${CLAUDE_PLUGIN_ROOT}" specky setup-diagrams $ARGUMENTS`). It's a one-time npm
install into specky's cache. Then suggest `/specky:doctor` to confirm `diagrams` is `ok`, and
`specky render-html` (or the `launch-viewer` skill) to see the diagrams drawn.
