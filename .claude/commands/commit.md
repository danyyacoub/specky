---
description: Commit this repo's changes — run the gates, sanity-check the diff, write a Conventional Commit message, and push when asked.
argument-hint: [optional hint about the change, or "push" / "merge to main"]
---

# Commit

`$ARGUMENTS`

Stage and commit the current work. Run every gate **before** writing the message, so the message
describes something that actually works.

## 1. Gather

Run in parallel:

```bash
git status --short
git diff --stat
git diff --staged
git log --oneline -8     # match the message style and the scope names already in use
```

Read the full diff for each changed file before deciding anything. If nothing changed, say so and
stop — do not create an empty commit.

## 2. Gates

| Change touches | Run |
|---|---|
| Anything under `src/`, `hooks/`, or `tests/` | `scripts/test.sh` — tmpdir-based, the faster failure |
| The engine CLI surface (`cli.py`, `indexer.py`, `db.py`, `html_render.py`, `generator.py`, `commit_doc.py`) | `scripts/smoke-test.sh` as well — it exercises the real CLI against this repo's own `specs/` tree, the one thing unit tests can't |
| Code that a `specs/<domain>/<topic>.md` describes | `uv run specky index && uv run specky check --base origin/main` — offline, and this is exactly what CI's `docs` job runs |
| Only docs, comments, or config | Skip the suite; say why |

`scripts/test.sh` passes extra arguments straight to pytest, so a scoped run is fine while iterating
(`scripts/test.sh tests/test_check.py -q`). A pre-commit hook that auto-fixes files and reports
"files were modified by this hook" is expected, not a failure: re-stage and re-run the same commit
once.

Do not claim the change works on a gate that did not run. `specky check` needs `specky index` to have
run first, and on a shallow history it passes vacuously — if either applies, say so instead of
reporting a green light.

## 3. Sanity-check the diff

Newly introduced lines only — never flag pre-existing code:

- **Debug leftovers**: `print(`, `breakpoint()`, `pdb.set_trace()`, commented-out code blocks. Not
  `print()` in `scripts/` or test fixtures, where it is often the output.
- **A committed key or config**: this repo's `specky.toml` and `.env` are gitignored, and nothing that
  ever holds a key may be staged. Providers read the key from the environment by the name
  `[ai] api_key_env` declares; specky itself never stores one.
- **Hand-edits under `specs/history/`**: never. One generated micro-doc per commit; a hand edit there
  is overwritten on the next sync.
- **A version bump**: `specky.__version__` and `.claude-plugin/plugin.json` move together, and only
  when cutting a release (see the Releasing section of `AGENTS.md`). A feature commit does not bump
  either; `tests/test_packaging.py` fails if they disagree.
- **A stale doc**: if this change alters behaviour a spec describes, update that spec in the *same*
  commit. The hook will write one afterwards, but a doc you know is wrong is yours to fix now.

Stage specific paths — `git add -A` will happily sweep in an untracked `assets/` or a scratch file.

## 4. Message

Conventional Commits, matching this repo's history:

```
type(scope): description
```

- **Types actually in use**: `docs` (the bulk — this repo documents itself), `feat`, `fix`,
  `refactor`, `chore`. The rest of the convention is available but unused here; prefer one of the five
  unless the change genuinely is a `test`, `perf`, `ci`, or `build` change.
- **Scopes in use**: the module — `rendering`, `viewer`, `chat`, `generator`, `document`, `cli`,
  `mcp`, `history`, `bootstrap`. A root-level change usually takes no scope at all.
- Imperative mood, lowercase, no trailing period, subject under 72 characters.
- Body only when the *why* isn't obvious from the subject; say why, not what. The diff says what.
- Separate logical changes into separate commits. Do not add `Co-Authored-By` or `Signed-off-by`.

Show the exact command and message, then ask before committing.

## 5. Commit, then let the hook be

The installed git hooks document the commit automatically. A `docs: sync specky docs [skip specky]`
commit appearing after yours is expected — leave it alone. Do not amend it into your commit, do not
squash it, and do not "fix" it unless its content is actually wrong (if it is, that's
`review-generated-docs`, as its own commit).

Set `SPECKY_DISABLE_HOOK=1` when the hook needs to stay out of the way. Verify with
`git log --oneline -3` and `git status --short`.

## 6. Push

Only when the user asked to push, or to merge. `push it`, `commit and push`, and `merge to main and
remove the local branch` are all explicit; a bare "commit this" is not. After pushing, report the
branch and what the remote now has — never force-push `main`.
