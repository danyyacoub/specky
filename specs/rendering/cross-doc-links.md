---
type: feature
tags: [rendering, documentation, search]
---

# Rendering — Cross Doc Links

## What It Does
Docs link to each other by repo path (like `../cli/check.md`), but the rendered viewer serves flat pages with no folders and no `.md` files. This feature resolves those in-body links at render time so they point at the page (or section) the target was actually rendered to, instead of 404ing.

## How It Works
1. **Pick the renderer's target** — Each renderer knows where it put a doc: the viewer serves one flat page per doc, while `specky export` serves one file with a section per doc.
2. **Resolve each link** — A resolver looks at the link's target and anchor and returns the correct href for that renderer, or nothing if the target isn't in the output.
3. **Unwrap dead links** — A link whose target the output doesn't contain keeps only its text; the broken anchor is dropped.
4. **Anchor headings** — Headings get ids so `#section` links have something to land on, with the content pane offset so the fixed titlebar doesn't cover them.
5. **Run before the sanitizer (answers only)** — Spec Assistant answers resolve links before the sanitizer runs, so a decoded `javascript:` href is still caught.
6. **Run after body rendering (export only)** — In `specky export`, resolution happens after the doc body is rendered, so heading ids don't interfere with the workflow stepper.

## Outcomes
| Situation | Result |
|---|---|
| Link targets a doc in the output | Resolved to that doc's page (viewer) or section (export) |
| Link has a `#section` anchor | Resolved to the matching heading id on the target page or section |
| Link targets a file the output doesn't contain | Link is unwrapped, keeping only its text |
| Link is not a doc link (page name, in-page anchor) | Kept as written |
| Answer link resolves to a `javascript:` href | Caught by the sanitizer that runs after resolution |

## Acceptance Tests
| Given | When | Then |
|---|---|---|
| A doc links another doc by repo path | The viewer renders the doc | The link points at the rendered page, not the `.md` file |
| A doc links another doc by repo path | `specky export` renders the file | The link points at the target's section |
| A link includes a `#section` anchor | The target doc is rendered | The link lands on the heading with that id |
| A doc links a file not included in the export (e.g. `specs/history/` without `--include-history`) | The export renders | The link is unwrapped, keeping only its text |
| A link names a page or an in-page anchor | The renderer processes it | The link is kept as written |
| An answer contains a `javascript%3A…` link | The answer is rendered | The sanitizer catches the decoded href after resolution |
