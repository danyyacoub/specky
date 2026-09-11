#!/bin/sh
# Fast dev loop for indexer/search changes: rebuild the FTS5 index, then search.
# Usage: scripts/reindex.sh [query]  (default query just confirms the index is non-empty)
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

uv run specky index
uv run specky search "${1:-specky}"
