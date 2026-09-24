---
description: Summarise the doc changes on this branch as a markdown PR comment — printed for you to read, posted only if you say so.
argument-hint: "[base revision, default origin/HEAD]"
allowed-tools: Bash(specky pr-comment:*), Bash(specky index:*), Bash(uv run --project:*)
---

# specky pr-comment

Base: `$ARGUMENTS`.

Run `specky pr-comment` from the repo root, adding `--base <that revision>` when one was given (or
run it through `uv run --project "${CLAUDE_PLUGIN_ROOT}" specky`). It only prints and never posts.
Show the markdown.

Posting is a separate step: offer `specky pr-comment … | gh pr comment <PR> --body-file -`, and run
it only when the user confirms which pull request it goes on.
