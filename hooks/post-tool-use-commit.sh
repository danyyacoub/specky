#!/bin/sh
# Convenience trigger only — the real mechanism is the git post-commit hook
# installed by `specky install-git-hook`, which fires regardless of which
# agent (or no agent) made the commit. This just gives faster feedback inside
# Claude Code itself right after it runs a `git commit` via the Bash tool.
#
# The plugin is installed per user, not per repo, so this fires after every Bash
# call in every repo. It only acts in a repo that opted in by running
# `specky init` (which writes specky.toml): anywhere else, `commit-doc` has no
# provider to call and would only print a config error and leave a `.specky/`
# behind in a repo that never asked for one.
#
# A substring match on the raw hook JSON rather than parsing it: this runs on
# every Bash call, and a false positive costs one `commit-doc` fire that finds
# nothing to do.
INPUT=$(cat)
case "$INPUT" in
  *"git commit"*)
    root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
    [ -f "$root/specky.toml" ] || exit 0
    # `install-git-hook --on merge|none` chose not to document per commit; this trigger mirrors
    # post-commit, so it steps aside with it.
    mode=$(git -C "$root" config --get specky.hooks 2>/dev/null)
    [ -z "$mode" ] || [ "$mode" = commit ] || exit 0
    command -v specky >/dev/null 2>&1 || exit 0
    OUT=$(specky commit-doc 2>&1) || true
    # With `provider = "agent"`, a commit made from this session is left for this session's agent
    # to document (the document-commits skill) instead of a headless `claude -p`. A plain hook's
    # stdout never reaches Claude, so the handoff line goes back as additionalContext.
    LINE=$(printf '%s\n' "$OUT" | grep '^specky: commits to document' | head -n 1)
    if [ -n "$LINE" ]; then
      LINE=$(printf '%s' "$LINE" | sed 's/\\/\\\\/g; s/"/\\"/g')
      printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"%s"}}\n' "$LINE"
    fi
    ;;
esac
exit 0
