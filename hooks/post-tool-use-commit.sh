#!/bin/sh
# Convenience trigger only — the real mechanism is the git post-commit hook
# installed by `specky install-git-hook`, which fires regardless of which
# agent (or no agent) made the commit. This just gives faster feedback inside
# Claude Code itself right after it runs a `git commit` via the Bash tool.
INPUT=$(cat)
CMD=$(printf '%s' "$INPUT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null)
case "$CMD" in
  *"git commit"*)
    command -v specky >/dev/null 2>&1 && specky commit-doc || true
    ;;
esac
exit 0
