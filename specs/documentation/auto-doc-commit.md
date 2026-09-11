---
type: workflow
tags: [documentation, sync]
---

# Documentation — Auto Doc Commit

## What It Does
The post-commit hook automatically stages and commits generated documentation (history, feature, and workflow docs) to the repository after each commit. Instead of leaving generated files as uncommitted changes, the hook creates a separate commit to capture them, identified by a special marker message to prevent infinite recursion.

## How It Works
1. **User commits**: When a commit is made to the repository, the post-commit hook runs.
2. **Check marker**: The hook checks if the current commit's message starts with the auto-commit marker (`docs: sync specky docs [skip specky]`). If it does, return immediately — this is the hook's own auto-commit.
3. **Generate docs**: The hook generates documentation for the commit, including a history file (`specs/history/<sha>.md`), and any feature/workflow docs from commit analysis.
4. **Stage changes**: The hook stages all changes under `specs/` using `git add specs`.
5. **Check for changes**: If nothing was written (no staged changes), the process ends without creating a commit.
6. **Commit updates**: The hook commits the staged docs with the auto-commit marker message.
7. **Report result**: If successful, the user sees "specky commit-doc: committed doc updates". If the commit fails (e.g., due to another hook rejection), the failure is printed but does not affect the original commit.

## Outcomes
| Scenario | Result |
|----------|--------|
| Docs generated successfully | New commit created with marker message, original commit unaffected |
| Nothing written (e.g., commit skipped by classification) | No auto-commit created, original commit unaffected |
| Auto-commit attempt fails | Error printed to output, original commit unaffected |
| Hook detects its own marker | Returns immediately without generating docs again |

## Acceptance Tests
| Given | When | Then |
|-------|------|------|
| User makes a regular commit | Post-commit hook runs | Hook generates docs, stages them, creates a follow-up commit with marker message |
| Post-commit hook creates auto-commit with marker | Hook runs for that auto-commit | Hook detects marker, returns immediately without recursion |
| No docs are generated (commit filtered by classification) | Hook stages specs/ | Hook finds no staged changes, skips creating a commit |
| Another hook rejects the doc commit | Hook attempts to commit staged specs/ | Original user commit remains; error is printed; user sees "doc updates written but not committed" message |
