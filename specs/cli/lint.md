---
type: feature
tags: [cli, ci]
sources: [src/specky/lint.py, src/specky/facts.py, src/specky/catalog.py, src/specky/cli.py]
---

# Cli — Lint

## What It Does

`specky lint` checks the docs **as a set**: what no single doc can show on its own. It reports three
kinds of drift:
- **terms** several docs use that `GLOSSARY.md` doesn't define;
- **tags** outside the repo's vocabulary;
- **numbers** that two docs about the same thing attach to the same name differently.

A single agent writing one careful doc can't see any of the three. The drift sits between that doc
and the thirty it didn't open. One agent-run migration kept every heading while dropping scoring
weights, formulas and two dozen glossary terms, and shipped all three kinds:
- *Price variance* and *To control* used across docs with no glossary row;
- fifteen tags carried by one doc each;
- a threshold stated two ways.

It's offline, and it reads the docs as they are on disk, uncommitted edits included, so an agent can
run it on the doc it just wrote, as the `document-commits` skill does after it writes. It needs no
index. Everything it reports is advice, and it never blocks a commit.
[`specky check`](check.md) runs the same checks over the docs a pull request is about.

## How It Works

1. **Load the docs** — every topic doc under the docs root, read from the worktree. `history/` and
the root's own files (GLOSSARY.md, MODULES.md, PRODUCT.md, TAGS.md) are skipped: they describe
the tree, not a feature. Paths given on the command line (files or directories) narrow which
docs findings may involve. Every doc is still read, because a term is shared or a number
conflicts only in comparison with the rest.
2. **Undefined terms** — collect what each doc uses *as a name*: bold terms that read like names
rather than emphasis, and the first column of any table under an Outcomes, Status, Result,
Classification, Diagnostic, Verdict or State heading. Those status names ("Price variance",
"To control") are never bolded, so this is the only way to see them. Report a term when:
   - `GLOSSARY.md` has no row for it, compared case- and plural-insensitively, with a row like
     "Tolerance (price)" also covering "Tolerance";
   - and at least two docs share it. A **bold phrase** is the author marking a term, so any doc
     mentioning it counts. A **status label**, or any single word, counts only in docs that also
     use it as a name. Outcomes tables are full of descriptive labels ("Left alone", "Nothing
     found"), and counting mentions of those, or of words like "Source", buried the real findings.

   Also not reported:
   - a section heading ("Acceptance tests"), which is template furniture;
   - a label written as code (`` `specky tags` ``);
   - either half of a "Entry / line item" row;
   - a code-shaped status (`COMPLIANT`) the glossary mentions anywhere, since it's a value of a
     defined term.
3. **Tags** — with a `TAGS.md` registry, report every tag outside it. Without one, report every tag
only one doc carries: it groups nothing. `document-domain` picks tags from `TAGS.md` when the repo
has one, and proposes a new tag rather than inventing one silently, so a stray tag here means a doc
skipped that step.
4. **Conflicting numbers** — pair each significant number in prose and table rows with the nearest
name within 40 characters, before or after it. A name is a glossary term, or a backticked
identifier shaped like a quantity: a snake_case or dotted field, or an ALL_CAPS constant. A flag
(`--yes`) or a command names an option, and two docs giving `--yes` different numbers were
describing two commands. Code blocks and example sections (Acceptance Tests) are skipped.

   For two docs that share a tag and state the same name, report the name when they have *nothing*
in common. Agreement is loose on purpose: either doc's claimed value appearing anywhere on the
other's lines about that name counts. "…or 50 (DPGF)" agrees with "DPGF over 30 PDF pages, or
more than 50 pages in total", even though that 50 sits too far from "DPGF" to be claimed for it.
5. **Report** — each kind as a block, fifteen items at a time, with every item in `--json`. It exits
0, or 1 with `--strict` when there's any finding.

## Flags

| Flag | Purpose |
|---|---|
| `PATH …` | Only report findings involving these docs or directories (default: all) |
| `--json` | Machine-readable output: `undefined_terms`, `tag_problems`, `conflicts` |
| `--strict` | Exit 1 when there is any finding |

## Outcomes

| Condition | Result |
|---|---|
| A phrase used in two or more docs with no glossary row | Listed under undefined terms, with the docs using it |
| A term with a glossary row, including via plural or a parenthesised variant | Not listed |
| A code-shaped status the glossary mentions in a definition | Not listed |
| A term only one doc uses | Not listed: it stays local to that doc |
| No `TAGS.md`; a tag carried by one doc | Listed as grouping nothing |
| A `TAGS.md`; a tag not in it | Listed as unregistered, however many docs carry it |
| Two docs sharing a tag give one name disjoint numbers | Listed as a conflict with each doc's value and line |
| Two docs share at least one value for the name | Not listed |
| Docs sharing no tag | Never compared |
| Nothing found | "Nothing to report."; exit 0 |
| Any finding with `--strict` | Exit 1 |

## Edge Cases

| Situation | What happens | Why |
|---|---|---|
| A dropped term is still used, but only in lowercase prose ("the tolerance is…") | Not reported | Nothing marks it as a name, and flagging every recurring phrase would bury the rest. `specky check` reports a glossary row a pull request deletes, and `specky adopt --verify` reports definitions a migration lost |
| The three kinds of wording conflict (a tie-break, a re-run's scope) | Not detected | No offline check reads a worded rule reliably. The skills' "one owner per rule" instruction is the defence. This check is the backstop for the kind a machine can compare |
| A number sits far from any name | It isn't claimed for it, but still counts toward agreement | Past 40 characters it usually belongs to another clause; if it matches the other doc's value, it's the same limit stated at more length |
| A doc was written a minute ago and isn't committed | It's included | Lint reads the worktree, which is why an agent can run it on its own doc |

## Acceptance Tests

| Scenario | Given | When | Then |
|---|---|---|---|
| Undefined phrase | "Price variance" a status in two docs' Outcomes tables, no glossary row | `specky lint` | Listed with both docs |
| Descriptive label | "Left alone" a status in one doc, plain prose in another | `specky lint` | Not listed |
| Furniture | **Edge cases** bold in two docs that also have `## Edge Cases`; `` `specky tags` `` as a status label | `specky lint` | Neither listed |
| Plural and code values | Glossary defines "Regular entry" and mentions COMPLIANT; docs use "Regular entries" and COMPLIANT | `specky lint` | Neither listed |
| Local term | **Refund window** in one doc only | `specky lint` | Not listed |
| Generic single word | "Source" is a status in one doc and a plain word in another | `specky lint` | Not listed |
| Sprawl without a registry | Tags `matching` (two docs), `manual-linking` and `billing` (one each) | `specky lint` | `manual-linking` and `billing` listed as single-doc |
| Registry | TAGS.md lists `matching` and `billing` | `specky lint` | Only `manual-linking` listed, as unregistered |
| Conflict | Two `matching` docs: "Tolerance is €0.01" and "Tolerance is 0.05" | `specky lint` | One conflict on `tolerance` with both values |
| Agreement | The second says "0.05, or €0.01 absolute" | `specky lint` | No conflict |
| Different tags | A `billing` doc says "tolerance is 12.5" | `specky lint` | Not compared |
| Examples ignored | The number sits in an Acceptance Tests table | `specky lint` | No conflict |
| Far value agrees | "Tolerance allows 20, or 50 in total" vs "Tolerance over 30 lines, or more than a grand total of 50 lines overall" | `specky lint` | No conflict |
| Flags aren't quantities | Two docs: "`--yes` … 25" and "`--yes` … 20" | `specky lint` | No conflict |
| Scope | `specky lint specs/billing` | Run | Only findings involving billing docs; `docs` is 1 |
| Strict | Any finding | `specky lint --strict` | Exit 1 |
| Clean tree | No docs | `specky lint` | "Nothing to report." |
