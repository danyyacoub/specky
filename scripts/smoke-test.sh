#!/bin/sh
# End-to-end sanity check of the CLI pipeline: index -> search -> render-html.
# Safe to run anytime — index/render-html are full rebuilds by design (see db.py),
# so this can't corrupt real state. Use before committing a change to any of the
# core engine modules (indexer.py, generator.py, html_render.py, db.py).
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

QUERY="${1:-specky}"

echo "==> specky index"
uv run specky index

echo "==> specky search \"$QUERY\""
uv run specky search "$QUERY"

echo "==> specky render-html"
uv run specky render-html

SITE=".specky/site/index.html"
if [ ! -f "$SITE" ]; then
  echo "FAIL: $SITE was not produced" >&2
  exit 1
fi

echo "PASS: index, search, and render-html all completed; $SITE exists"
