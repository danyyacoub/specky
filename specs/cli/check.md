---
type: workflow
tags: [ci, documentation]
---

# Cli — Check

## What It Does

`specky check` is the CI gate that fails if a pull request changes code that a doc describes without updating that doc. It costs nothing to run — no AI provider, no history walk — and answers from the diff and the `doc_files` table `specky index` derives from git. The gate asks for one honest doc update per file, not a checklist: if a file is covered by multiple docs, updating any one of them is sufficient.

## How It Works

1. **Resolve the range.** Use `--base REV` if provided; otherwise `--since REV|DATE`, or default to `origin/HEAD` (in CI) or `HEAD~1` (locally).
2. **Diff the range.** Compare against merge base with `git diff --name-only <base>...HEAD`, so files the target branch changed after this one forked don't count.
3. **Split changed files.** Paths under `specs/` are doc updates. Everything else is code, minus the ignore list (default: `.github/`, `.specky/`, markdown outside `specs/`, lockfiles, `*.txt`, `*.cfg`, `*.ini`).
4. **Look up covering docs.** Query `doc_files` for which docs describe each changed file.
5. **Check for violations.** For each changed code file, a violation occurs if none of its covering docs were updated in the range, and no sibling doc in the same domain was added. Pairs must recur across `[check] min_link_commits` (default 2) separate commits to fail.
6. **Report results.** Violations fail the build; undocumented files and missing `specs/history/` docs generate warnings but never fail.

## Configuration

Add to `pyproject.toml` or `specky.toml`:

```toml
[tool.specky.check]
ignore = [".github/", "*.lock"]   # replaces defaults wholesale
min_link_commits = 2              # commits a file/doc pair must recur in before enforced
```

Policy in `pyproject.toml` is shared with CI; `specky.toml` is gitignored and local-only.

## Outcomes

| Condition | Behavior |
|-----------|----------|
| Changed code's covering doc was updated in the range | Exit 0 |
| A different doc in the same domain was updated | Exit 0 |
| File covered by multiple docs; any one was updated | Exit 0 |
| Code changed, no covering doc touched, pair seen ≥`min_link_commits` commits | Violation; exit 1 |
| Same violation with `--advisory` | Report printed; exit 0 |
| Same violation with `--json` | JSON payload; exit 1 |
| Pair seen in only one commit | Counted as coverage; note printed; never fails |
| Changed file has no doc | Warning; exit 0 |
| Covering doc since deleted | Not demanded; exit 0 |
| Commit without `specs/history/` doc | Warning; exit 0 |

## Acceptance Tests

| Scenario | Given | When | Then |
|----------|-------|------|------|
| Doc updated alongside code | File covered by a doc; both changed | Run `specky check` | No violations; exit 0 |
| Doc skipped | Same file changed, doc untouched | Run `specky check` | Violation; exit 1 |
| Multiple docs, one updated | File described by two docs; one changes with the code | Run `specky check` | No violation; exit 0 |
| Advisory mode | Violating range | Run `specky check --advisory` | Same output; exit 0 |
| JSON mode | Violating range | Run `specky check --json` | JSON payload with violations; exit 1 |
| Weak link | File/doc pair from one commit; file changes later | Run `specky check` | No violation; pair counts as covered |
| Strict threshold | Same, with `min_link_commits = 1` | Run `specky check` | Violation |
| Undocumented file | Changed file no doc covers | Run `specky check` | Warning; exit 0 |
| Deleted doc | Covering doc removed since linked | Run `specky check` | No violation; exit 0 |
| Missing history | Commit without `specs/history/` entry | Run `specky check` | Warning; exit 0 |
| Merge-base semantics | Base branch changed a file after this branch forked | Run `specky check` | File not in diff; no violation |
| Ignore list applied | Range changes only `.github/` and `README.md` | Run `specky check` | No code files counted |
| Policy in committed file | `[tool.specky.check]` in `pyproject.toml` only | Run `specky check` in CI | Policy applies; shared with CI |
| Same-domain sibling doc | Range changes code and adds new doc in that domain | Run `specky check` | No violation |
