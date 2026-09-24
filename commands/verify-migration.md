---
description: After rewriting an old docs tree into specky's, list every number, formula and defined term the old docs stated that no new doc does.
argument-hint: "<glob of the old tree, e.g. 'specs/**' or 'docs/**'>"
allowed-tools: Bash(specky adopt --verify:*), Bash(uv run --project:*)
---

# Verify a docs migration

Old tree: `$ARGUMENTS`. If empty, ask which directory held the old docs, and stop.

Run from the repo root, saving the report:

```bash
specky adopt --verify --only '$ARGUMENTS' > specky-migration-report.md
```

If `specky` isn't on PATH, use `uv run --project "${CLAUDE_PLUGIN_ROOT}" specky` instead. It writes
and moves nothing apart from the report, and makes no AI call. It pairs each old doc with the doc
that replaced it (`origin:` frontmatter first, then the path an import would have used, and a
root `GLOSSARY.md` / `PRODUCT.md` / `MODULES.md` with the docs root's own). A fact found in some
other new doc counts as moved, not missing.

Then summarise `specky-migration-report.md`:
- The **missing constants** per doc: numbers, formulas, glossary definitions. These are what a
  rewrite loses silently. For the first few, check the code and say whether it still applies
  them. If it does, the new doc should state them (its `## Constants & Invariants` section).
- Old docs with **no counterpart**, and where their content should live.
- Missing *names* only as a count. They're mostly helper and field names a functional doc doesn't
  need.

Offer to restore the facts the code still applies. Edit nothing until the user says so.
