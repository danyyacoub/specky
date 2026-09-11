#!/bin/sh
# Build the static viewer, open it, and run the chat companion in the foreground.
# Equivalent to the "browse the docs" steps in README.md, in one command.
# Ctrl+C stops the chat server; the rendered site keeps working without it.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> specky render-html"
uv run specky render-html

SITE="$ROOT/.specky/site/index.html"
if command -v open >/dev/null 2>&1; then
  open "$SITE"
else
  echo "Open $SITE in a browser"
fi

echo "==> specky serve (Ctrl+C to stop)"
exec uv run specky serve "$@"
