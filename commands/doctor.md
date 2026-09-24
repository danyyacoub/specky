---
description: Diagnose specky in this repo — toolchain, provider config, git hooks, index, diagrams, doc backlog — and say how to fix each problem.
allowed-tools: Bash(specky doctor:*), Bash(uv run --project:*)
---

# specky doctor

Run `specky doctor` from the repo root. If `specky` isn't on PATH, run
`uv run --project "${CLAUDE_PLUGIN_ROOT}" specky doctor` instead.

Then report:
- Every `[fail]` first, then every `[warn]`, each with the one command or edit that fixes it. The
  detail line usually names it (`specky install-git-hook`, `specky setup-diagrams`, `specky index`).
- Nothing about `[ok]` lines unless asked, beyond "everything else is fine".

Don't run the fixes yourself. Offer them, and run one only when the user says so. Several of them
(hooks, provider config) change how every later commit behaves, and the `setup` skill walks
through them with the user.
