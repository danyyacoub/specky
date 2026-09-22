---
name: find-feature
description: Before using a piece of this project's functionality, find out what it is meant to do. Use when about to call, depend on, extend or integrate with a feature, endpoint, command or module and asking for its intended use, inputs and outputs, guarantees or limits — "before I call X, what is it meant to do?", "what are the rules for X?", "is X allowed to do Y?". Answers from the repo's specky docs with citations, without reading source. Not for broad "how does X work?" or "why did it change?" questions (use explore-docs), and not for finding where code lives.
---

# Find Feature
State what a piece of functionality is *meant* to do before someone uses it, citing the repo's
functional docs (`specs/<domain>/<topic>.md`). The docs hold intent; the code holds what runs.
This is the docs-only lookup — read source only if step 5 sends you there.

## Which model runs this
If you are running as a subagent, skip this section. Otherwise read `model` from the `[skills]`
table of `specky.toml` at the repo root. No file, no table, no key, or `inherit`: follow the steps
yourself. If it names a model and you can start a subagent on a named model (Claude Code's Agent
tool takes `model`), start one on it, give it the path of this `SKILL.md` and the user's question,
and relay its answer. If you can't, or the subagent fails, follow the steps yourself.

## Steps

### 1. Check there are docs
Docs live under the docs root (`specs/` unless `[docs] root` in `specky.toml` says otherwise). If
the repo has no such tree, say so in one line and go to step 5.

### 2. Find the doc
- `search_docs` with the feature or symbol name as the user wrote it.
- Miss: retry with the terms `specs/GLOSSARY.md` uses for the concept.
- Still miss: `list_features` and `list_domains`, pick by name.

### 3. Read it
`read_doc` on the best hit. If two docs claim it, read both and prefer the more specific one.

### 4. Get the exact promises
`doc_behaviours` on that doc — its stable ids: STEP-n (a step), OUT-n (an outcome), EDGE-n (an edge
case), AT-n (an acceptance test). These are the intended behaviour. Quote the ids.

### 5. Answer
One sentence first: what it is meant to do. Then:
- **Inputs** and **Outputs**, from the doc's steps.
- **Guarantees and limits** — a two-column table, rule | what the docs say happens. A limit with no
  doc row is not a promise; say so rather than guessing.
- **Cite** the doc path and the STEP-n/OUT-n/EDGE-n/AT-n ids the answer rests on.

If the docs don't cover it, say so in one line, search the code for the symbol, and name the gap: it
is worth a doc (`document-domain` skill, or `specky document "<feature>"`). Never invent behaviour
the docs don't state.

## Without the MCP tools
`specky search "<query>"` (after `specky index`), then read the matching files under the docs root.
