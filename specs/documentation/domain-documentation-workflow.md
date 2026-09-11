# Domain Documentation Workflow

## What It Does
When you need to document a feature, module, or domain in the codebase, this workflow guides you through discovering related code, understanding the functionality, and writing a functional spec that non-technical stakeholders can read. The result is a markdown file in `specs/` that stays consistent with the project's glossary and product framing.

## How It Works

1. **Identify the domain** — you provide the module name (e.g., "billing", "search", "auth") or specific files to document.
2. **Discover related code** — search the codebase for all files matching the domain name and identify the key services, components, routers, or jobs.
3. **Check existing specs** — look in `specs/<domain>/` to see if this topic is already documented; if so, update it instead of creating a duplicate.
4. **Load the shared vocabulary** — read `specs/PRODUCT.md` (product framing) and `specs/GLOSSARY.md` (canonical term definitions) to ensure consistency.
5. **Understand the functionality** — read the main implementation files to identify: what the feature does, the sequence of steps it follows, and what outcomes it produces.
6. **Write the functional doc** — create or update a markdown file in `specs/<domain>/` using kebab-case topic names (not `README.md`) with sections for: What It Does, How It Works, Outcomes or Statuses, and Acceptance Tests.
7. **Update shared glossary if needed** — if the domain introduces a genuinely new term other specs will need, add it to `specs/GLOSSARY.md`.

```mermaid
flowchart TD
    A[1. Identify domain] --> B[2. Discover related code]
    B --> C{3. Existing spec?}
    C -->|Yes| D[Update existing file]
    C -->|No| E[Plan new file]
    D --> F[4. Load PRODUCT.md + GLOSSARY.md]
    E --> F
    F --> G[5. Understand functionality]
    G --> H[6. Write the functional doc]
    H --> I{7. New glossary term?}
    I -->|Yes| J[Add to GLOSSARY.md]
    I -->|No| K[Done]
    J --> K
```

## Outcomes

| Outcome | Meaning |
|---------|---------|
| New spec created | `specs/<domain>/<topic>.md` written with all required sections |
| Existing spec updated | Found and updated `specs/<domain>/<topic>.md` to reflect current implementation |
| New glossary term added | Domain introduced a concept that is now defined in `specs/GLOSSARY.md` |
| Consistency verified | All terms used match existing glossary; product framing aligns with `PRODUCT.md` |

## Acceptance Tests

| Scenario | Given | When | Then |
|----------|-------|------|------|
| First-time domain doc | No existing `specs/<domain>/` | User requests docs for "billing" | Workflow discovers billing code, creates `specs/billing/`, writes markdown with all sections; non-technical reader understands what the feature does |
| Existing doc update | `specs/billing/invoicing.md` exists but is outdated | User requests docs for "billing" | Workflow finds existing file, updates How It Works and Acceptance Tests to match current implementation |
| New term introduced | Domain uses concept "pro-rata credit" not in `specs/GLOSSARY.md` | Doc is written | Workflow prompts to add "pro-rata credit" to glossary; term is defined and used consistently in both the new doc and glossary |
| Glossary reuse | "pro-rata credit" already in `specs/GLOSSARY.md` | Domain doc references the concept | Workflow reuses the exact glossary definition; no synonym or alternate wording is invented |
