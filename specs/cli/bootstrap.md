---
type: workflow
tags: [cli, documentation, sync]
authored: human
sources: [src/specky/bootstrap.py]
---

# Cli — Bootstrap

## What It Does

Writes a repository's first documentation by reading its **source code**, rather than by replaying
its commit history. It runs on its own the first time `specky sync` meets a repo with no feature
docs, and can be run directly at any time afterwards.

This is the one part of specky that reads code. Everything else reads git, which is the right shape
once docs exist — a diff is a precise statement of what changed about a feature already described
somewhere — and the wrong shape for a repo that has none. Left to the commit path, a module written
in one commit years ago and untouched since gets no doc at all, and what does get written is
assembled from whatever diffs happened to touch it.

## How It Works

1. **Build a map of the repo** — No AI call. Files come from `git ls-files`, so anything gitignored
   or untracked is excluded by construction rather than by a denylist per ecosystem. The map has
   four tiers: the directory tree, the package manifests, the README, and an index of each file's
   header line and top-level definitions.

2. **Budget the map** — Only the symbol index grows with file count, so only it is capped. It is
   filled round-robin across directories — every directory's best file, then every directory's
   second-best — because a domain nobody was shown is a domain nobody can find. Tests and
   tracked-but-vendored trees rank last. Whatever didn't fit is stated in the prompt, so a partial
   map isn't read as a complete one.

3. **Ask what the repo is** — One call returns the product framing and the domain list, each domain
   naming the paths it lives in. Docs that already exist are listed, and the model is told to reuse
   their exact `domain`/`topic` rather than name a near-duplicate. Directory prefixes are asked for
   instead of file lists, and the list is capped, so the answer can't overflow the output limit —
   which would abandon the run after paying for its most expensive call.

4. **Check what it named is real** — Each domain's paths are resolved against the file list; a
   prefix expands to the files under it, and anything that doesn't resolve is dropped. A domain left
   with no source at all is skipped **before** any call is made. This is the guard against the worst
   failure available here: a doc written from no source reads exactly like a real one.

5. **Write `PRODUCT.md`** — Rendered in Python from the previous answer, with no call of its own.
   Never overwritten if it already exists: it is the framing every other doc is written against, and
   the doc a team is most likely to have written deliberately.

6. **Write one doc per domain** — One call each, shown that domain's own source within a budget,
   most central files first, using the same style template the commit path uses. The doc records the
   files it was written from in a `sources:` frontmatter key.

7. **Write `GLOSSARY.md`** — One call, made *after* the docs exist so the terms are read back off
   real documentation rather than guessed from a file listing. Additive only: existing definitions
   win, existing prose is untouched, and a term carrying a character the viewer's parser can't read
   is dropped rather than escaped.

8. **Hand back to the commit walk** — `specky sync` then documents the last ten commits. They are
   already reflected in docs just written from the working tree, so they earn a `specs/history/`
   entry and nothing more — no classification, no doc rewrite.

## Where The Docs Come From

| Path | Reads | AI calls | Overwrites an existing file? |
|---|---|---|---|
| `specs/<domain>/<topic>.md` | that domain's source | 1 per domain | No — a domain with a doc is skipped |
| `specs/PRODUCT.md` | the discovery answer | 0 | No |
| `specs/GLOSSARY.md` | the docs just written | 1 | No — appends unseen terms only |
| `specs/MODULES.md` | — | 0 | No — a row is added only if the doc is linked nowhere |

## Resuming

There is no state file. Discovery re-runs each time — one cheap call — and its answer is diffed
against what is on disk, which matters because `.specky/` is gitignored and a marker kept there
would claim "never bootstrapped" on every fresh clone.

So a domain that already has a doc drops out, `--max-domains` caps how much of what's left one run
takes on, and re-running walks the rest. That terminates: every run either writes a doc or reports
there is nothing left. Deleting a doc is how you ask for it to be rewritten.

## Outcomes

| Condition | Behaviour |
|---|---|
| No feature docs and some source to read | `specky sync` offers the bootstrap, then walks the commits |
| No feature docs and no source at all | Not offered — an all-prose or empty repo isn't cold, it's empty |
| Some domains already documented | Only the undocumented ones are written |
| Every domain already documented | Says so; costs the discovery call and nothing else |
| More domains than `--max-domains` | The cap is documented, the remainder is reported, re-running continues |
| A path scope is given | Discovery and docs are limited to that subtree |
| A domain whose paths don't resolve | Skipped and reported; no call is paid for it |
| A generated doc names a `--flag` this repo doesn't accept | Refused, parked in `.specky/pending/`, reported |
| One domain's generation fails | Reported and skipped; the rest of the run continues |
| Discovery hits the output token limit | Fatal for the run, with the `max_tokens` lever named |
| `--dry-run` | Lists the plan and stops — still costs the discovery call, and only that |
| `--no-bootstrap` on `sync` | Skipped entirely; the commit path behaves as it always did |
| Anything is written | Left uncommitted, like `specky sync` — a run that writes a whole doc tree is a change a human should read first |

## Cost

O(domains), not O(commits): **2 + N calls**, where N is the domains documented this run. Roughly
$0.18 on an average repo including the ten commits, against ~$89 for the full-history backfill that
was the only broad-coverage option before it. Halved again with `--batch`, and the discovery call
can be pointed at a stronger model on its own — see
[ai/provider-cost-controls.md](../ai/provider-cost-controls.md).

## Doc Coverage

Docs written here record their source files in `sources:`, and `specky index` folds those pairs into
the code-to-doc map. Without it a bootstrapped repo would have **no** `specky check` coverage at
all: that map is otherwise derived from git log, and a single commit adding twenty docs is exactly
what its per-commit limits exist to reject. The commit path preserves the key across later updates.

## Acceptance Tests

| Scenario | Given | When | Then |
|---|---|---|---|
| Cold repo bootstraps first | A repo with source and no feature docs | `specky sync` | Docs are written from the code, then the commits are walked |
| Warm repo never does | One doc at `specs/billing/refunds.md` | `specky sync` | No bootstrap is offered and no discovery call is made |
| History docs don't count as warm | Only `specs/history/*.md` and `specs/MODULES.md` exist | Cold-start is checked | Still cold — neither is a feature doc |
| An empty repo isn't cold | A repo with no source files | Cold-start is checked | Not cold; nothing is announced and nothing is read |
| Bootstrapped commits cost one call each | A cold repo with two commits | `specky sync` | History entries are written; no commit is classified and `.specky/pending/` stays empty |
| A second run adds only what's new | Three domains, first run capped at two | `specky bootstrap` again | The third doc is written; the first two are untouched |
| A third run stops | Every domain documented | `specky bootstrap` | Says every domain already has a doc; no doc call is made |
| The cap reports the remainder | Three domains, `--max-domains 2` | `specky bootstrap --max-domains 2` | Two docs written, one reported as deferred |
| Existing docs steer discovery | `specs/billing/refund-flow.md` exists | `specky bootstrap` | The discovery prompt lists it, so the model reuses it rather than naming a twin |
| An invented path costs nothing | Discovery names `src/nonexistent/` | `specky bootstrap` | The domain is skipped, no doc is written, and only the discovery call was paid for |
| A doc sees only its own source | Two domains with different files | `specky bootstrap` | Each doc's prompt contains its own domain's code and not the other's |
| A huge file is cut, and says so | A domain whose source exceeds the budget | `specky bootstrap` | The prompt carries a truncation notice rather than ending mid-file |
| `PRODUCT.md` is never overwritten | A hand-written `specs/PRODUCT.md` | `specky bootstrap` | Its contents are unchanged |
| Hand-written glossary definitions win | `Refund` already defined by hand | Bootstrap proposes a new definition for it | The hand-written one survives; the proposed one is not added |
| A generated glossary row round-trips | Bootstrap writes a glossary | The viewer's parser reads it back | Every term is parsed, including one whose definition contains a pipe |
| Scoping limits discovery | Source under `src/billing/` and `src/auth/` | `specky bootstrap src/billing` | Only the billing subtree is read |
| A gitignored tree is invisible | A gitignored `node_modules/` | The map is built | None of its files appear |
| Unreadable files don't abort the run | A binary `.py`, a minified bundle, a symlink, a CP1252 source file | The map is built | Each is skipped and the map still builds |
| The map stays inside its budget | Six hundred symbol-heavy files | The map is built | The symbol index is within its cap and states how many files it left out |
| Every directory is represented | Three hundred files in one directory, one in another | The map is built | The lone file still appears |
| One failing domain doesn't abandon the rest | A provider that fails for one domain | `specky bootstrap` | That domain is reported and skipped; the others are written |
| Dry run writes nothing | A cold repo | `specky bootstrap --dry-run` | The plan is printed, no files appear, and exactly one call is made |
| Declared sources give check coverage | A bootstrapped repo, committed and indexed | The code-to-doc map is read | Each source file is paired with the doc that named it |
| A later update keeps `sources:` | A doc with a `sources:` key | A commit updates it through the normal path | The key survives |
