---
type: workflow
tags: [cli, ci]
---

# Cli — Check

## What It Does

`specky check` is the CI gate that turns documentation from something that happens into something that's enforced. It answers one question about a range of commits: *did this change touch code that a doc describes, without updating that doc?* Each answer is a **violation** and fails the build.

No AI provider is ever called and history is never walked, so it costs nothing and runs in milliseconds on a pull request. It answers from two things that already exist: the diff for the range, and the `doc_files` table `specky index` derives from git.

It fails by default — a doc gate that only warns is a doc gate nobody notices — with `--advisory` to print the identical report and exit 0, which is how a repo adopts the gate before it's clean.

## How It Works

1. **Resolve the range.** `--base REV` wins. Otherwise `--since` picks it: a revision is used as-is, and a date (`"2 weeks ago"`) resolves to the *parent* of the oldest commit in that window, so that commit's own changes aren't silently dropped. With neither, `origin/HEAD` is the default when it resolves — in CI that's the branch a pull request targets — falling back to `HEAD~1` locally, and to git's empty-tree hash in a repo with one commit.
2. **Diff it** with `git diff --name-only <base>...HEAD`. Three dots, not two: the comparison is against the merge base, so a pull request isn't blamed for files the target branch changed after it forked.
3. **Split the changed files.** Paths under `specs/` are the *doc updates* being checked for. Everything else is candidate code, minus the ignore list (`[check] ignore`, defaulting to `.github/`, `.specky/`, markdown outside `specs/`, lockfiles, `*.txt`, `*.cfg`, `*.ini`).
4. **Look up covering docs** in `doc_files` — one indexed SQL query for the whole diff, joined against `documents` so a doc that has since been deleted can't be demanded.
5. **A violation is a covering doc whose whole domain the range left untouched**, reported as `src/specky/db.py → specs/search/fts5-syntax-safety.md`, and only when the file/doc pair recurs across at least `[check] min_link_commits` separate commits (default 2). Pairs below that threshold count as coverage but are only tallied as a note — see *Why recurrence* below. Updating any doc under `specs/<domain>/` satisfies every covering doc in that domain: a domain is specky's unit of functional grouping, and a change most often documents itself by adding a *new* sibling doc, which can't be in `doc_files` yet because nothing has linked it to a commit.
6. **Report the advice that never fails the build**: commits in the range with no `specs/history/` doc (the hook probably isn't installed), changed files with no covering doc at all, weak links that weren't enforced, and the coverage figure. Those are states a repo grows into, not regressions a contributor introduced.

## How Coverage Is Derived

The file → doc map is precomputed by `specky index`, and derived **from git alone**:

- `.specky/` is gitignored, so anything read out of that database instead — `commit_links`, which the post-commit hook writes — is empty on a fresh clone. A map built from it would leave `specky check` with nothing to enforce in CI, the one place the gate runs. Deriving from committed history means a CI run and a developer's checkout compute the same map.
- **The walk**: `git log --name-only -- specs/` yields every commit that touched a doc, with its parents and subject. Path-limited, so both the walk and the output stay proportional to the number of doc-producing commits rather than to the length of history.
- **Whose code those docs describe**: specky's flow splits the two — code lands in one commit and the hook's follow-up commits the docs. The `specs/history/<sha>.md` files in a doc commit name the commits they document, and one `specky sync` backfill can carry hundreds. Failing that, an auto-commit's docs belong to the commit it followed, and any other commit (a hand-written doc, an agent that committed code and docs together) describes itself.
- **The file lists**: one `git cat-file --batch-check` resolves the history docs' abbreviated shas (a rebase can leave one dangling or ambiguous — those are dropped, not fatal), then one `git log --no-walk --stdin --name-only` fetches the file lists. Three git processes total, none proportional to history.
- Doing it the other way — `git log -- <file>` per changed file at check time — costs a git process per file in the diff and rescans all of history on every run. Fine on a small repo, not on a monorepo with a 300-file pull request.
- **Two symmetric fan-out bounds.** A commit touching more than `DOC_FILES_MAX_PER_COMMIT` (50) files contributes none of them: a bulk rename, a vendored-dependency drop or an initial import names thousands, and a doc in that commit isn't evidence it describes each one. A commit rewriting more than `DOC_FILES_MAX_DOCS_PER_COMMIT` (3) covering docs likewise contributes nothing — that's a batch regeneration or a bulk doc import, not one change being documented. On specky's own repo, three such commits accounted for 12 of 13 reported violations.
- Per-commit history docs and the root indexes (`MODULES.md`, `GLOSSARY.md`, `PRODUCT.md`) never count as covering a code file; they'd otherwise pair everything with everything. specky's own outputs (`specs/`, `.specky/`) are never counted as covered code.

### Why Recurrence

Git says a commit produced a doc; it doesn't say which of that commit's files the doc is about. The map therefore pairs the doc with the incidental test file and CLI plumbing alongside the code it's actually describing. A second, independent commit pairing the same two is what distinguishes a doc that tracks a file from a coincidence.

Measured on specky's own repo, that threshold cut a 6-commit range from 9 reported violations (several plainly wrong) to 1 correct one. The consequence is deliberate leniency on a young repo: with only one doc-producing commit per file, nothing fails. `[check] min_link_commits = 1` opts into the stricter behaviour once a repo's history is dense enough.

## Flags

| Flag | Purpose |
|------|---------|
| `--base REV` | Compare against `REV` (default: `origin/HEAD` if it resolves, else `HEAD~1`) |
| `--since REV\|DATE` | e.g. `v1.2.0` or `"2 weeks ago"` |
| `--advisory` | Print the same report but always exit 0 |
| `--json` | Machine-readable output for PR annotations — still exits 1 on a violation |

## Configuration

`[check]` in `specky.toml`, or `[tool.specky.check]` in `pyproject.toml`. The second spelling exists because `specky init` writes a **gitignored** `specky.toml` — it holds provider config — so a policy kept only there doesn't exist in CI. `pyproject.toml` is committed, so that's where a policy the team and CI share belongs; `specky.toml` still wins locally when it has a `[check]` table.

```toml
[tool.specky.check]
ignore = [".github/", "*.lock"]   # replaces the defaults wholesale; a bare string is accepted
min_link_commits = 2              # separate commits a file/doc pair needs before it's enforced
```

## Outcomes

| Condition | Behavior |
|-----------|----------|
| Changed code's covering doc was updated in the range | Clean; exit 0 |
| A different doc in the covering doc's domain was updated | Clean — documenting the change in the right domain is enough |
| Changed code's covering doc wasn't updated, nor anything in its domain | Violation naming file and doc; exit 1 |
| Same, with `--advisory` | Byte-identical report; exit 0 |
| Same, with `--json` | JSON payload; still exit 1 |
| The pair was seen in only one doc-producing commit | Counted as coverage, tallied as a note, never a failure |
| A changed file has no doc at all | Warning and a coverage figure; never a failure |
| The covering doc has since been deleted | Not demanded |
| A commit in the range has no `specs/history/` doc | Warning pointing at `install-git-hook` and `specky sync` |
| specky's own `docs: sync specky docs [skip specky]` commits | Never counted as undocumented |
| The target branch moved on after this branch forked | Those files aren't in the diff, so they can't be violations |
| A repo with a single commit | Compared against the empty tree — the whole worktree is the range |
| `--since` names a window with no commits | Empty range; nothing changed, nothing to check |
| `--base` isn't a revision in this repo | One stderr line, no traceback; exit 1 |

## In CI

```yaml
- run: uv run specky index
- run: uv run specky check --base origin/main
```

The checkout needs `fetch-depth: 0`: both the base revision and the `specs/`-limited walk the map is built from need real history, and a shallow clone has neither. specky's own doc-sync commits land in the same pull request range, so a repo with the hook installed passes without extra work.

## Acceptance Tests

| Scenario | Given | When | Then |
|----------|-------|------|------|
| Doc updated alongside code | A file covered by a doc; the range changes both | Run `specky check` | No violations; exit 0 |
| Doc skipped | The same file changed, doc untouched | Run `specky check` | One violation naming file → doc; exit 1 |
| Advisory mode | The same violating range | Run `specky check --advisory` | Identical output; exit 0 |
| JSON mode | The same violating range | Run `specky check --json` | Payload carries violations and coverage; exit 1 |
| Weak link isn't enforced | A file/doc pair from a single commit; that file changes | Run `specky check` | No violation; the pair still counts as covered; a note reports it |
| Stricter threshold | Same, with `min_link_commits = 1` | Run `specky check` | That pair is a violation |
| Undocumented file | A changed file no doc covers | Run `specky check` | Warning and 0% coverage; exit 0 |
| Deleted doc | A covering doc removed from the repo since it was linked | Run `specky check` | No violation |
| Missing history docs | A commit in the range with no `specs/history/` entry | Run `specky check` | Warning naming the commit; exit 0 |
| Merge-base semantics | Base branch changed a file after this branch forked | Run `specky check` | That file isn't in the diff and can't be a violation |
| Ignore list | The range changes only `.github/` and `README.md` | Run `specky check` | No code files counted |
| Ignore list is configurable | `[check] ignore = ["vendor/"]` | Load the config | `vendor/` is ignored and the defaults are gone |
| Policy lives in a committed file | `[tool.specky.check]` in `pyproject.toml`, no `specky.toml` | Load the config | The policy applies, so CI shares it |
| Local override | Both files carry a `[check]` policy | Load the config | `specky.toml` wins |
| Same-domain sibling doc | The range changes covered code and adds a new doc in that doc's domain | Run `specky check` | No violation |
| Another domain's doc | The range changes covered code and updates a doc in an unrelated domain | Run `specky check` | Violation |
| Fresh clone | A repo cloned with no `.specky/` | Run `specky index` | The coverage map is identical to the source repo's |
| Backfill commit | One doc-sync commit whose history docs name several older commits | Run `specky index` | Each named commit's files are paired with the docs |
| Atomic commit | One commit carrying both code and its doc | Run `specky index` | That commit's files are paired with that doc |
| Dangling history doc | A history doc naming a sha that isn't in the repo | Run `specky index` | It's skipped; the run still completes |
| Bulk commit | A doc in a commit touching more than 50 files | Run `specky index` | None of those files are recorded as covered |
| Batch doc rewrite | A commit rewriting more than 3 covering docs | Run `specky index` | It contributes no pairs |
| specky's own outputs | A doc-producing commit that touched only `specs/` or `.specky/` | Run `specky index` | No coverage rows recorded |
| Reindexing | Any indexed repo | Run `specky index` twice | The map is rebuilt, not accumulated |
| Bad base | `--base v9.9.9-nope` | Run `specky check` | One stderr line, no traceback; exit 1 |
