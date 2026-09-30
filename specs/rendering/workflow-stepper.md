---
type: feature
tags: [rendering, documentation]
---

# Rendering — Workflow Stepper

## What It Does
On workflow docs, the numbered happy-path list under the "How It Works" section is rendered as a visual stepper instead of a plain numbered list. The stepper uses bold labels on each step as titles. Lists that don't fit the expected shape are left as plain lists.

## How It Works
1. **Find the section.** The renderer locates the `<h2>` whose text is "how it works" and treats everything up to the next `<h2>` as that section's body.
2. **Find every list in the section.** Each `<ol>` within the section is considered separately — not just one immediately after the heading.
3. **Check each list's shape.** A list is promoted only if it has no nested lists and at least one step carries a bold label to use as a title; otherwise it stays a plain list.
4. **Promote the qualifying lists.** Each qualifying `<ol>` is rewritten as `<ol class="steps">` with each step's bold label lifted into a `<span class="st">` title.
5. **Leave everything else alone.** Lists outside the "How It Works" section, and lists inside the section that don't match the shape, are untouched.

## Outcomes
| Situation | Result |
| --- | --- |
| List directly under `## How It Works` with bold labels | Stepper |
| List after a lead-in sentence in that section | Stepper |
| Multiple lists under `###` subheadings in that section | One stepper per qualifying list |
| List in that section without bold labels | Plain list |
| List with nested lists | Plain list |
| List outside the "How It Works" section | Plain list |
| Doc that isn't a workflow | Plain list |

## Constants & Invariants
- Heading match: section heading text, stripped of markup and lowercased, must equal `how it works`.
- Section boundary: a section runs from its `<h2>` to the next `<h2>` (any `<h2>` opening, i.e. `<h2>` or `<h2 ...>`).
- Promotion applies only when `doc_type == "workflow"`.
- "Labelled" means at least one list item contains a bold label (`<strong>…</strong>`) to split on; if none do, the list is left as-is.
- Promotion is skipped entirely if the list contains a nested list.
- Each qualifying list is promoted independently; a section may yield zero, one, or several steppers.

## Maintainer Notes
- `_STEP_SECTION` matches an `<h2>` (capturing `open` and `head`) plus `body`, where `body` is tempered against the next `<h2[ >]` — not just `</h2>` — so the first heading on the page doesn't swallow subsequent sections.
- `_STEP_LIST` matches `<ol>…</ol>` inside a section; `_STEP_ITEM` matches `<li>…</li>`; the bold-label split uses the group produced by `link_glossary`.
- `STEP_HEADING = "how it works"` is the canonical comparison value.
- The modifier is applied only when `doc_type == "workflow"`; feature docs are deliberately excluded so their numbered lists don't get promoted.
- CSS for `.steps` and `.st` lives in the stylesheet; the renderer only emits the markup.

## Acceptance Tests
| Given | When | Then |
| --- | --- | --- |
| A workflow with a list immediately under "How It Works" with bold labels | Rendering the site | The list renders as a stepper |
| A workflow whose "How It Works" opens with "Two steps then run in order:" before the list | Rendering the site | The list renders as a stepper, and the lead-in sentence is preserved |
| A workflow whose "How It Works" has separate `###` paths, each with its own labelled list | Rendering the site | Each qualifying list renders as its own stepper |
| A workflow with a labelled list in a section other than "How It Works" | Rendering the site | The list stays a plain list |
| A workflow whose list has no bold labels | Rendering the site | The list stays a plain list |
| A non-workflow doc with a labelled list under "How It Works" | Rendering the site | The list stays a plain list |
