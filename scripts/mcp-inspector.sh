#!/bin/sh
# Launch the MCP Inspector against specky's own MCP server (src/specky/mcp_server.py),
# so new tools can be exercised interactively instead of guessing at stdio framing.
# Requires Node/npx. Mirrors the launch command in .mcp.json.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

exec npx @modelcontextprotocol/inspector uv run --project "$ROOT" specky-mcp
