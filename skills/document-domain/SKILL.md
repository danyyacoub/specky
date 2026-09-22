---
name: document-domain
description: Generate or update functional documentation for a domain/module of the current codebase, keeping specs/MODULES.md and specs/GLOSSARY.md in sync. Use when asked to document a domain, module, feature, or write/update specs.
---

# Document Domain
Generate or update functional documentation for a domain module.

## Inputs
Ask the user for:
1. **Domain name** — which module/area to document (e.g., "accounts", "billing", "search", "auth").
2. **Related files** (optional) — specific files or directories to focus on. If not provided, discover them by searching the codebase for the domain name.

## Which model runs this
If you are running as a subagent, skip this section. Otherwise read `model` from the `[skills]`
table of `specky.toml` at the repo root. No file, no table, no key, or `inherit`: follow the steps
yourself. If it names a model and you can start a subagent on a named model (Claude Code's Agent
tool takes `model`), ask the user for the Inputs first — a subagent can't ask them — then start one
on it, give it the path of this `SKILL.md` and those answers, and relay what it wrote and changed.
If you can't, or the subagent fails, follow the steps yourself.

## File Naming Convention
- Functional docs live in **`specs/<domain>/`**, mirroring the codebase's own module layout where one exists. Cross-cutting docs stay at the `specs/` root.
- File name = a kebab-case slug of the **topic** (not `README.md`). Prefer the module name (e.g., `billing.md`) or a descriptive topic (e.g., `refund-flow.md`, `rate-limits.md`).
- One `.md` per topic. If a module has multiple distinct topics, split into multiple files.
- If the domain folder doesn't exist yet under `specs/`, create it.

## Doing it without an agent

`specky document "<feature or workflow>"` runs this whole procedure unattended: it hands a model
tools to search and read the repo, then writes the doc, the `MODULES.md` row and any new glossary
terms, applying the same rules below. It needs `[ai]` pointed at a provider with a tool channel
(`anthropic`, or an `openai-compatible` endpoint that supports tool calling).

Follow the steps below when you are the agent doing the work — you have the repo in context and
your own tools, which is usually the better doc. Either way the output is the same shape, and
`specky document` is the right answer when someone wants it done in CI or without an agent.

## Steps

### 1. Discover the domain
- Find all related files: `find . -path "*<domain>*" -not -path "*/node_modules/*" -not -path "*/.venv/*" -not -path "*/dist/*"`
- Identify the relevant modules, services, routers, jobs, or components for this domain.
- If the user provided specific files, use those as the primary scope.
- Ignore vendored and generated trees even when they are committed (`vendor/`, `third_party/`,
  `*_pb2.py`, minified bundles) — they are not this repo's own code.

### 2. Check existing documentation
- Look in `specs/<domain>/` for an existing functional doc (kebab-case `.md` — see Naming Convention above). Avoid creating a second doc for the same topic.
- If files exist, read them to determine if they need updating.

### 3. Consult the shared vocabulary
Before reading code, load the canonical references so the doc stays consistent with the rest of `specs/`:
- **`specs/PRODUCT.md`** — what this product is and who it is for; the product framing every doc is written against.
- **`specs/GLOSSARY.md`** — shared term definitions. Reuse these exact terms and their meanings; do **not** invent a synonym for a concept that already has a glossary entry. If the domain introduces a genuinely new term that other specs will need, note it (see step 8).

### 4. Understand the functionality
- Read the main service files, routers, and key components.
- Identify: what the feature does, how it works (high-level steps), what outcomes it produces.
- For frontend: identify pages, components, API calls.
- For backend: identify endpoints, services, key calculations.
- Note the concrete input → output behaviours worth pinning down as acceptance tests (see the template's Acceptance Tests section).

### 5. Get the type of functionality — feature, workflow, or specific case
- **Workflow**: identify the sequence of steps and the expected outcome for each step.
- **Feature**: identify the specific functionality and its expected outcomes.
- **Specific case**: identify the unique scenario and its expected outcome.

### 6. Write or update the functional doc
Create/update the doc in `specs/<domain>/` using the descriptive filename (Naming Convention above), following this style:

```markdown
---
type: feature
tags: [kebab-case-business-concept]
---

# {Domain Name} — {One-line purpose}

## What It Does
{2-3 sentences explaining the feature in plain language. Non-technical people should understand this.}

## How It Works
{Numbered steps explaining the process. Each step is one sentence with a bold label. On a
`type: workflow` doc this is the happy path and nothing else — see Workflow docs below.}

{Diagram, if this doc qualifies — see Diagrams rules below. Placed right after the numbered steps.}

## {Outcomes / Statuses / Results}
{Table showing possible outcomes and their meaning}

## Edge Cases
{`type: workflow` only, and required there. Table: Situation | What happens | Why — every branch off
the happy path: what is refused, what is skipped silently, what a partial run leaves behind. If there
genuinely are none, say so in one line rather than omitting the section.}

## Acceptance Tests
{Scenario-based tests that pin down the expected behaviour. One row per scenario, Given/When/Then style.
Cover the happy path, key edge cases, and any tolerance/threshold boundaries. Use the exact terms from
GLOSSARY.md so a scenario is unambiguous.}

| Scenario | Given | When | Then (expected) |
|---|---|---|---|
| {short name} | {starting state / inputs, using glossary terms} | {action or trigger} | {observable outcome / computed value} |
```

**Workflow docs have a fixed shape** (`type: workflow`):
- `## How It Works` is the **happy path only** — the run where everything goes right, numbered in
  the order it happens. Anything conditional, any failure and any refusal belongs in
  `## Edge Cases`, not as an aside inside a step.
- A ` ```mermaid ` diagram is **always** required, directly under the numbered steps.
- `## Edge Cases` is required, as a Situation | What happens | Why table.
- `## Acceptance Tests` closes the doc and carries a row for every `## Edge Cases` row.
- A `type: feature` doc keeps the shape above and has neither requirement.

**Frontmatter rules**:
- `type`: `feature` for step 5's "Feature" or "Specific case" types, `workflow` for "Workflow".
- `tags`: 1-3 kebab-case tags naming the business/domain concept (e.g. `billing`, `refunds`), not
  implementation details. Run `specky tags` first and reuse an existing tag if one fits — tags are only
  useful for search/grouping if they're shared across docs, not invented per-doc.
- `related` (optional): add `related: [domain/topic]` only when this doc is genuinely tied to another
  one that shares no tag — e.g. a workflow that calls into a feature from a different domain. Leave it
  out otherwise; shared tags already cover most links and show up in `specky graph`.
- `owner` (optional): never write one. It's a hand-written "who to ask" line (a name, team or
  channel) that the viewer surfaces, and you have no way to know who that is.
- `sources` (recommended): the code files you actually read to write this doc. `specky index` folds
  these into the code-to-doc map, and it is the only thing giving a doc written from code (rather
  than from a commit) any `specky check` coverage — that map is otherwise derived from git log, and
  a doc about a module nobody has touched in months has no commit pairing it with that module.
  List only files you really read.
- If updating an existing doc, keep its `related` list and `owner` as-is unless they're actually
  wrong now — both are hand-authored, not something to regenerate from scratch.

**Acceptance-tests rules**:
- **Always include an Acceptance Tests section.** If the domain is purely descriptive with no testable behaviour, say so explicitly in that section rather than omitting it.
- Each scenario is concrete: real-ish values, named entities, expected outputs.
- Cover: happy path, at least one edge case, and every tolerance/threshold/boundary the domain has.
- Use the exact vocabulary from `GLOSSARY.md`, and state the formula inline when a `Then` is a computed number.
- Prefer scenarios that map to (or already have) real tests in the codebase's test suite — link them when they exist.

**Style rules**:
- Plain language, no jargon unless necessary.
- Compact — no verbose explanations.
- Focus on WHAT and WHY, not implementation details.
- Tables for structured information.
- No code blocks unless showing a formula, threshold, or a diagram (see below).
- Understandable by non-technical stakeholders.

**Diagram rules**:
- **Workflow** docs (step 5's "Workflow" type): always include a diagram of the sequence.
- **Feature** or **specific case** docs: include a diagram only when it earns its space —
  the process branches into a real decision tree (not just a flat outcomes table), or
  distinct roles/actors (user, service, external system, another domain) hand off to each
  other. A single actor doing a straight sequence of steps doesn't need one; the numbered
  list already covers it.
- Use a fenced ` ```mermaid ` block — renders natively on GitHub and most markdown viewers,
  degrades to readable text everywhere else.
- Pick the diagram type to match the shape: `flowchart` for branching/decision logic,
  `sequenceDiagram` for multiple roles/actors exchanging steps. Don't use both for the same
  doc.
- Keep node/actor labels short and reuse exact `GLOSSARY.md` terms — the diagram is a map of
  the same steps in "How It Works", not a separate source of truth. If the two drift, the
  numbered list wins.
- Skip it if the doc's type doesn't qualify — a diagram on every doc is clutter, not clarity.

### 7. Update the `MODULES.md` index
Open `specs/MODULES.md` at the repo root and keep the index current. It is a set of `| Doc | Purpose |` tables — one section per domain, in whatever order best reflects the product.
- If the doc is new, add a row in its domain's section with the link and a one-line purpose. If the domain has no section yet, add one, with a short paragraph describing the bounded context above its table.
- If the doc was renamed or moved, update the link.
- If the doc was deleted, remove the row.
- Keep each section's rows ordered consistently with the rest of the section.
- Cross-module relationships belong in an architecture doc (if one exists), not in `MODULES.md` — if step 4 turned up a new dependency between domains, update that doc instead.

### 8. Keep `GLOSSARY.md` in sync
- If documenting the module surfaced a **new shared term** other specs will reuse, add it to `specs/GLOSSARY.md` in the right section (definition only, no implementation detail).
- If a term is domain-specific and unlikely to be reused, keep it in the doc only and note it in the summary.
- Downstream tooling (this plugin's indexer and HTML viewer) reads `MODULES.md` and `GLOSSARY.md` back out and treats them as the authority on the doc set's hierarchy and vocabulary, so both must stay accurate.

### 9. Handle updates
If documentation already exists:
- Read the current code to check if functionality has changed.
- Compare with existing doc content.
- Only update sections that are outdated or missing (including the Acceptance Tests section
  and, per the Diagram rules above, a missing or now-stale diagram).
- Preserve any manually-added context that's still accurate.
- Report what was updated and why.

### 10. Summary
Report:
- Files created or updated (functional doc, `MODULES.md`, `GLOSSARY.md`).
- Whether a new `MODULES.md` section was added, and where the doc was placed.
- Whether any glossary term was added.
- Any areas that need manual review (e.g., complex business logic that needs stakeholder input).
