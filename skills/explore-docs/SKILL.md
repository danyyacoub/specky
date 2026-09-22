---
name: explore-docs
description: Answer questions about how this project behaves from its specky docs and git history, before reading source code. Use when the user asks what a feature does, how a flow or workflow works, what its rules, statuses, limits or edge cases are, whether something is supported, or why something changed. For example "how do refunds work?", "what happens when a payment fails?", "do we support X?", "why did we change Y?". Not for finding where a function, class or file is defined.
---

# Explore Docs
Answer a behaviour question from the repo's functional docs (`specs/<domain>/<topic>.md`) and the
git history specky indexed alongside them, and cite where each part of the answer comes from.

The docs are written for this: what a feature does, in its own terms, with its outcomes, edge
cases and acceptance tests. Reading them first is faster and cheaper than working the behaviour out
from code, and it gives you the intended behaviour, which code alone can't. It isn't a substitute
for the code when the question is about the code itself. Hand those questions to code search.

| Question | Where the answer is |
|---|---|
| What does X do? How does Y work? What happens when Z? | The docs: this skill |
| Why is it like this? When did it change? | Git history, through specky: this skill |
| Where is X implemented? What calls Y? | The code: use code search, not this skill |
| Does the code still do what the doc says? | Both: step 4 below |

## Which model runs this
If you are running as a subagent, skip this section. Otherwise read `model` from the `[skills]`
table of `specky.toml` at the repo root. No file, no table, no key, or `inherit`: follow the steps
yourself. If it names a model and you can start a subagent on a named model (Claude Code's Agent
tool takes `model`), start one on it, give it the path of this `SKILL.md` and the user's question,
and relay its answer. If you can't, or the subagent fails, follow the steps yourself.

## Steps

### 1. Find the docs
- First make sure the repo has specky docs at all: a docs tree (`specs/` unless `[docs] root` says
  otherwise) with markdown in it. If it doesn't, this repo doesn't use specky. Say so in one line
  and answer from the code instead. Don't run `specky index` or `specky init` to create one; that's
  the user's call (`/specky:setup`).
- `search_docs` with the user's words. If that misses, retry with the terms `specs/GLOSSARY.md`
  uses for the concept; the docs use those exact terms.
- For "what exists" questions ("which features touch billing?", "what workflows do we have?"), use
  `list_domains`, `list_features`, `list_workflows` or `list_tags` instead of searching.
- If `search_docs` says nothing is indexed, run `specky index` (no AI call) and search again.

### 2. Read them
- `read_doc` on the best one to three hits. The frontmatter carries `type`, `tags`, `related` (docs
  worth following) and `sources` (the code the doc was written from).
- For "exactly what should happen when…" questions, `doc_behaviours` returns the doc's promises with
  stable ids: STEP-n, OUT-n, EDGE-n, AT-n. Quote the id, e.g. "EDGE-2 in
  specs/billing/refund-flow.md", so the user can find it and a later change can name it.

### 3. Find the why, when asked
`search_history` for the change by topic, or `commits_for_doc` for everything that touched one doc.
Each commit carries its message and a summary of what changed.

### 4. Check the doc against the code when it matters
Docs state intended behaviour and can lag the code. Check before the answer is used to change code,
or when the user is asking what the code does *now* rather than what it's meant to do:

- Compare the doc's last commit with its sources':
  `git log -1 --format=%cs -- <doc>` against `git log -1 --format=%cs -- <each file in sources>`.
  A source changed after the doc means the doc may be stale; read that source to confirm.
- If code and doc disagree, say so plainly. The code is what runs; the doc is what was intended.
  Don't pick one silently. Offer to update the doc (the `document-domain` skill).

For a plain "how does X work?" with no change in sight, the doc is the answer. Don't audit the code
for every question.

### 5. Answer
- Lead with the short answer: the fact, rule or yes/no, and the doc it comes from. Then the
  details, with a table when you're comparing cases or statuses.
- Cite every doc path (and behaviour id) the answer rests on. Say what you verified in code, if
  anything.
- Don't fill gaps from general knowledge. If the docs don't cover it, say so, then answer from the
  code if the user needs an answer now, and point out the gap: it's a doc worth writing
  (`document-domain` skill, or `specky document "<feature>"`).

## Without the MCP tools
If the `specky` MCP server isn't connected, the CLI covers the same ground: `specky search
"<query>"` for step 1 (after `specky index`), then read the matching files under the docs root
directly. That's `specs/` unless `[docs] root` in `specky.toml` says otherwise. Use `git log` on a
doc for its history.
