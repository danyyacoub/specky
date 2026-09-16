# Smart Commit
Review staged/unstaged changes, run targeted review subagents, then generate a conventional commit.

## Steps

### 1. Gather the changes
1. Run `git diff --cached` to see staged changes. If nothing is staged, run `git diff` to see unstaged changes and report what would need to be staged.
2. Run `git diff --cached --name-only` (or `git diff --name-only` if nothing staged) to list changed files. This file list drives which subagents run in step 3.
3. Read the full diff for each changed file before delegating, so you can give the subagents accurate context.

### 2. Inline quick checks
Scan the diff yourself for the cheap, high-signal issues (newly-introduced lines only):

   ### Leftover Debug Code
   - `console.log`, `console.debug`, `print()`, `debugger`, `binding.pry`, `pdb.set_trace`, `breakpoint()`
   - Commented-out code blocks (more than 2 consecutive commented lines that look like code, not documentation)

   ### TODO / FIXME / HACK Comments
   - Any `TODO`, `FIXME`, `HACK`, `XXX`, or `TEMP` comments introduced in the diff (new lines only, ignore removed or pre-existing ones)

   ### Secrets & Obvious Risk
   - Hardcoded secrets, tokens, credentials, connection strings
   - Obvious injection risk (raw SQL string interpolation, `eval`, command injection)

### 3. Targeted review subagents
Spawn the applicable subagents **in parallel** (independent calls in a single message) so the review is fast. Pass each one: the list of changed files, the full diff, and an instruction to review **only newly-introduced code** in the diff (never pre-existing code). Each subagent is **read-only** — it reports findings, it does not edit or commit.

Apply a **confidence filter** to every subagent: report only issues the agent is genuinely confident matter. Prefer a short list of real problems over an exhaustive list of nitpicks. No issues found is a valid, expected result.

**a. Performance subagent — CONDITIONAL.**
Run **only if** the changed files touch the data layer, i.e. any of:
   - a file matching `*_repository.py`
   - a diff that adds/modifies SQLAlchemy query code (`select(`, `session.execute`, `.join(`, `selectinload`, `joinedload`, `.options(`, relationship access, `func.`, `text(`)
   - a new or changed Alembic migration in `migrations/versions/`

   Use subagent_type `general-purpose`. Ask it to check for:
   - N+1 query patterns (querying inside a loop, lazy relationship access in a loop)
   - Missing eager loading (`selectinload`/`joinedload`) where relationships are accessed
   - Queries selecting full ORM objects when only a scalar/column is needed
   - Missing or unused indexes for new filter/join/order-by columns (cross-check `api/models/db_models.py` and migrations)
   - Unbounded queries (no pagination/limit on potentially large result sets)
   - `flush()`/`commit()` placement that breaks the repo convention (repositories `flush()`, services own `commit()` — see `.claude/rules/backend.md`)
   - Work done in Python that the DB should do (aggregation, filtering, sorting)

**b. Code quality / refactoring subagent — when non-trivial code changed.**
Use subagent_type `code-simplifier:code-simplifier`. Ask it to identify concrete simplifications and cleanups in the newly-changed code:
   - Duplicated logic that should be extracted
   - Over-complex conditionals/nesting that can be flattened
   - Dead code, unused imports/variables introduced in the diff
   - Naming, readability, and adherence to project conventions in `.claude/rules/` (domain-module layout, Pydantic returns over tuples, `ApiErrorException` + `ErrorCode`, `log_tenant`/`log_system`)
   - Functions doing too much that should be split
   It should return a prioritized list of suggested refactors with file/line references — suggestions only, no edits.

**c. Regression subagent — always (when code changed).**
Use subagent_type `feature-dev:code-reviewer`. Ask it to determine whether the changes could break existing behavior:
   - Changed function/method signatures, return types, or removed fields — find and check all callers
   - Renamed/removed exports, schema fields, or DB columns still referenced elsewhere
   - Behavioral changes to shared/utility code that other modules depend on
   - Broken or now-incorrect tests, and logic paths that lost test coverage
   - Frontend/backend contract drift (a changed API response shape vs. its TS consumer)
   It should report concrete regression risks with the specific file/line of the affected caller, not hypotheticals.

If only docs/config/trivial changes are present, skip the subagents and note why.

### 4. Report findings
Aggregate the inline checks and all subagent results into one clear list, grouped by category (Performance / Quality / Regression / Debug / Secrets), each citing file path and line. De-duplicate overlapping findings. Then:
- **If issues are found**: present them and ask whether to proceed with the commit, fix them first, or commit a subset. Do not auto-fix unless the user asks.
- **If no issues (or the user chose to proceed)**: continue to step 5.

### 5. Generate the commit message
Follow Conventional Commits:
   - Format: `type(scope): description`
   - Types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `style`, `perf`, `ci`, `build`
   - Scope: the primary module or area affected (e.g., `api`, `frontend`, `auth`, `quotes`)
   - Description: imperative mood, lowercase, no period, max 72 chars
   - Add a body (separated by blank line) if the change is non-trivial, explaining **why** not **what**
   - If multiple logical changes exist, suggest splitting into separate commits
   - Do not add co-authored-by or Signed-off-by lines

### 6. Commit
1. Show the proposed commit message and ask for confirmation.
2. Stage the relevant files (prefer specific files over `git add -A`) and create the commit.
3. **If pre-commit hooks abort the commit by auto-fixing files** (e.g. `end-of-file-fixer`, `trailing-whitespace`, `black`, `isort` report "files were modified by this hook"): this is expected, not a failure. Re-stage the modified files (`git add` them again) and re-run the same commit once. If the hooks pass clean on the retry, the commit succeeds. Only treat it as a real failure if a hook reports an error it cannot auto-fix (e.g. a lint/type error) — surface that to the user instead of retrying.
4. Run `git status` after committing to verify success.

### 7. Follow-up reminders
After a successful commit, remind the user to run:
- **`/add-tests`** — to add or update unit tests for the logic introduced in this commit.
- **`/document`** — to update the functional docs and Claude rules for any domain affected.

Only surface the reminder that's relevant: skip `/add-tests` if no testable logic changed (docs/config/trivial only), and skip `/document` if no domain behavior changed.

## Guidelines
- Only flag issues that are **newly introduced** in the diff — do not flag pre-existing code. This applies to the inline checks AND every subagent.
- Run the subagents concurrently; never block one on another. They are advisory — the user decides what to act on.
- Be pragmatic: a `TODO` with a ticket reference (e.g., `TODO(ZI-123)`) is acceptable. Favor a few real findings over noise.
- Do not flag test files for debug statements like `print()` — those are often intentional.
- Keep the commit message concise. If the diff is large, focus the description on the most important change.
- When in doubt about scope or type, ask the user.
