#!/bin/sh
# Report the state of this repo's specky setup: toolchain, config, and whether the
# git post-commit hook is installed. Run this before debugging "why didn't a doc
# get generated" instead of manually checking each piece.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ok()   { printf '  [ok]   %s\n' "$1"; }
warn() { printf '  [warn] %s\n' "$1"; }

echo "== toolchain =="
if command -v uv >/dev/null 2>&1; then
  ok "uv: $(uv --version)"
else
  warn "uv not found on PATH — see https://docs.astral.sh/uv/"
fi
python3 --version 2>/dev/null | sed 's/^/  [ok]   /' || warn "python3 not found"
if command -v node >/dev/null 2>&1; then
  ok "node: $(node --version)"
else
  warn "node not found — only needed for rendering ```mermaid``` diagrams (optional)"
fi

echo "== mermaid diagram rendering =="
# Ask specky where it resolved the tool to rather than guessing a path: an installed specky uses
# ~/.cache/specky, a checkout uses vendor/ (see src/specky/mermaid_tool.py).
TOOL_DIR="$(uv run python -c 'from specky.mermaid_tool import tool_dir; print(tool_dir() or "")' 2>/dev/null || true)"
if [ -n "$TOOL_DIR" ]; then
  ok "mermaid renderer installed at $TOOL_DIR"
else
  warn "not set up (diagrams fall back to plain text) — run: uv run specky setup-diagrams"
fi

echo "== config =="
if [ -f specky.toml ]; then
  ok "specky.toml present"
  sed 's/^/         /' specky.toml
else
  warn "specky.toml missing — run: uv run specky init"
fi

echo "== git hook =="
HOOK=.git/hooks/post-commit
if [ -f "$HOOK" ] && grep -q "specky" "$HOOK"; then
  ok "post-commit hook installed at $HOOK"
else
  warn "post-commit hook not installed — run: uv run specky install-git-hook"
fi

echo "== index =="
if [ -f .specky/index.db ]; then
  ok ".specky/index.db present"
else
  warn "no index yet — run: uv run specky index"
fi
