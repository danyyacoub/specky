---
name: document-commits
description: Write specky's history docs for the commits it reports as pending, and update the feature docs they change. Use when specky's hook output says "specky: commits to document … run the document-commits skill", or when asked to document recent commits or catch up specky's commit docs in this session.
---

# Document Commits
Document the commits specky is waiting on, in this session, instead of specky launching a headless
copy of this agent to do it. Each commit gets a history doc (`specs/history/<sha>.md`); a commit
that changes a documented feature also gets that feature doc updated.

specky hands this over only when `[ai] provider = "agent"` and the commit came from inside that
agent's own session. Everywhere else (a terminal commit, CI, an API provider) its git hook still
does the work itself.

## Which model runs this
If you are running as a subagent, skip this section. Otherwise read `model` from the `[skills]`
table of `specky.toml` at the repo root. No file, no table, no key, or `inherit`: follow the steps
yourself. If it names a model and you can start a subagent on a named model (Claude Code's Agent
tool takes `model`), start one on it, give it the path of this `SKILL.md`, and relay what it wrote
and committed. If you can't, or the subagent fails, follow the steps yourself.

## Running specky
Use `specky` if it's on PATH, else the plugin's own copy:

```bash
run_specky() { if command -v specky >/dev/null 2>&1; then specky "$@"; else uv run --project "${CLAUDE_PLUGIN_ROOT}" specky "$@"; fi; }
```

## Steps

### 1. List what's pending
```bash
run_specky pending --json
```
`commits` lists the commits to document, oldest first. `rules` is the exact instruction specky
gives a provider for a history entry. Follow it to the letter. If `commits` is empty, say so and
stop.

Work through at most 5 commits unless the user asked for more. Older ones stay pending, and
`specky sync` catches up a long backlog.

### 2. For each commit, oldest first
1. **Read it.** Run `git show --stat --patch <sha>`. On a large diff, the stat and the parts that
   change behaviour are enough.
2. **Write the micro-doc** as the JSON object `rules` asks for, with keys `headline`, `impact`,
   `what_changed` and `why`. Describe what the product does differently, in its users' terms.
   Leave `why` empty when neither the commit message nor the diff states a motivation.
3. **Find the feature it belongs to**, if any. Check `specs/MODULES.md`, then run `search_docs` (or
   `run_specky search "<terms>"`) with the commands, settings or screens the commit changed. A
   commit with `impact: internal` usually belongs to none.
   - If it changes behaviour an existing doc describes, update that doc. Follow steps 2–9 of the
     `document-domain` skill (`skills/document-domain/SKILL.md`), touching only the sections the
     commit made wrong or incomplete. Its Constants rules matter most here: keep every threshold,
     formula and precedence order the commit didn't change, and update the ones it did — `specky
     check` names each constant a doc stopped stating. Take tags from `specs/TAGS.md` when the repo
     has one, and state a rule another doc owns by linking to it (One owner per rule).
   - If it adds a feature no doc covers, write one the same way, including its `MODULES.md` row.
   - Otherwise leave the feature docs alone.
4. **Record it.** Pass the JSON on stdin, and `--feature` with the repo-relative path of the doc
   from 3, if there is one:
   ```bash
   run_specky record-commit <sha> --feature specs/<domain>/<topic>.md <<'JSON'
   {"headline": "...", "impact": "...", "what_changed": "...", "why": "..."}
   JSON
   ```
   It writes the history doc, indexes it and prints its path.

Never hand-write or edit anything under `specs/history/`. `record-commit` is the only writer.

### 3. Lint what you wrote
If you changed any feature doc, run `run_specky lint <those docs>`. Fix what it names that your
change caused — a glossary row for a term other docs also use, a registered tag, a number that
disagrees with the doc owning it — and mention anything left in the report. It's advice, and it
never blocks the commit.

### 4. Commit the docs
Stage exactly what this run wrote: the history docs `record-commit` printed, any feature doc you
changed, and `specs/MODULES.md` / `specs/GLOSSARY.md` / `specs/TAGS.md` if you changed them. Then
commit:

```bash
git add <those paths>
git commit -m "docs: sync specky docs [skip specky]"
```

That subject is specky's own marker. The hook recognizes it and doesn't document the docs commit.
Leave any other uncommitted work alone.

### 5. Report
For each commit: its short sha, the headline you wrote, and the feature doc it linked to, if any.
Also report which feature docs were created or updated, and how many commits are still pending.
