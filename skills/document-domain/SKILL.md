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

## File Naming Convention
- Functional docs live in **`specs/<domain>/`**, mirroring the codebase's own module layout where one exists. Cross-cutting docs stay at the `specs/` root.
- File name = a kebab-case slug of the **topic** (not `README.md`). Prefer the module name (e.g., `billing.md`) or a descriptive topic (e.g., `refund-flow.md`, `rate-limits.md`).
- One `.md` per topic. If a module has multiple distinct topics, split into multiple files.
- If the domain folder doesn't exist yet under `specs/`, create it.

## Steps

### 1. Discover the domain
- Find all related files: `find . -path "*<domain>*" -not -path "*/node_modules/*" -not -path "*/.venv/*" -not -path "*/dist/*"`
- Identify the relevant modules, services, routers, jobs, or components for this domain.
- If the user provided specific files, use those as the primary scope.

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
# {Domain Name} — {One-line purpose}

## What It Does
{2-3 sentences explaining the feature in plain language. Non-technical people should understand this.}

## How It Works
{Numbered steps explaining the process. Each step is one sentence with a bold label.}

## {Outcomes / Statuses / Results}
{Table showing possible outcomes and their meaning}

## Acceptance Tests
{Scenario-based tests that pin down the expected behaviour. One row per scenario, Given/When/Then style.
Cover the happy path, key edge cases, and any tolerance/threshold boundaries. Use the exact terms from
GLOSSARY.md so a scenario is unambiguous.}

| Scenario | Given | When | Then (expected) |
|---|---|---|---|
| {short name} | {starting state / inputs, using glossary terms} | {action or trigger} | {observable outcome / computed value} |
```

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
- No code blocks unless showing a formula or threshold.
- Understandable by non-technical stakeholders.

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
- Only update sections that are outdated or missing (including the Acceptance Tests section).
- Preserve any manually-added context that's still accurate.
- Report what was updated and why.

### 10. Summary
Report:
- Files created or updated (functional doc, `MODULES.md`, `GLOSSARY.md`).
- Whether a new `MODULES.md` section was added, and where the doc was placed.
- Whether any glossary term was added.
- Any areas that need manual review (e.g., complex business logic that needs stakeholder input).
