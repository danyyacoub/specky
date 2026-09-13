---
type: feature
tags: [documentation]
---

# Cli — Export

## What It Does

`specky export` packages your docs into a single self-contained file you can email, print, or upload to your company wiki. It reuses the viewer's rendering pipeline so the exported content matches exactly what readers see in the browser — same tables, same diagrams, same glossary definitions. By default it creates a no-JavaScript HTML file; you can also generate a PDF or Confluence storage format.

## How It Works

1. **Run the export command** with optional flags to pick your output format (the single page is the default and has no flag of its own; `--pdf` prints that page, `--confluence` writes the other shape instead, and the two are mutually exclusive) and whether to include history docs (`--include-history`).
2. **The tool reads the index** and fetches every doc from the database, sorting them by domain.
3. **Each doc is rendered** using the same markdown-to-HTML pipeline the viewer uses, including glossary term hovers and server-side mermaid diagram rendering.
4. **For single-page HTML**: all docs are inlined into one file with a domain-grouped table of contents and a print stylesheet; no JavaScript is included so the file stays portable.
5. **For PDF** (if `--pdf` flag and `weasyprint` installed): the single-page HTML is printed to PDF; if weasyprint isn't available, the HTML is still written and a message tells you what to install.
6. **For Confluence** (if `--confluence` flag): each doc is converted to Confluence storage-format XHTML — no mermaid rendering at all (the fence's source goes into a code macro instead), glossary spans and table wrappers unwound, and fenced code put in an `<ac:structured-macro>` whose CDATA body holds the source *un*escaped, since storage format keeps a CDATA section verbatim. One flat `<slug>.xhtml` per doc, plus an index page listing them.
7. **The command reports** the files it wrote, the doc count, and (if history was excluded) how many per-commit docs were left out and how to include them. A total size is reported only when the result is big enough to be a surprising attachment.

## Outcomes

| Scenario | Output | File Location |
|----------|--------|---------------|
| Default (single page) | One no-JS HTML file with TOC, every doc inlined except `specs/history/`, print stylesheet | `.specky/export/specky-docs.html` |
| With `--pdf` flag | HTML file + PDF (if weasyprint installed) | `.specky/export/specky-docs.html` + `.specky/export/specky-docs.pdf` |
| With `--confluence` flag | One XHTML file per doc + index page, flat and named after the doc's page slug | `.specky/export/<slug>.xhtml`, `.specky/export/index.xhtml` |
| Both `--pdf` and `--confluence` | Command fails with an argparse error naming the conflict — one shape would otherwise be dropped silently | Nothing written |
| With `--include-history` | Same as above, plus all per-commit notes from `specs/history/` | Included in chosen output format |
| `--title "Custom Title"` | Any format above with custom title on cover and in browser tab | Sets document title (default: "Documentation") |
| No index yet | `RuntimeError` telling you to run `specky index` first | Nothing written |

## Acceptance Tests

| Given | When | Then |
|-------|------|------|
| A repo with 10 docs across 3 domains and 50 commits | Running `specky export` | A single HTML file is written to `.specky/export/specky-docs.html` with all 10 docs grouped by domain, no JavaScript, and a print stylesheet |
| The same repo | Running `specky export --include-history` | The export includes all 50 per-commit docs from `specs/history/` alongside the 10 main docs |
| The same repo and `weasyprint` is installed | Running `specky export --pdf` | Both `.specky/export/specky-docs.html` and `.specky/export/specky-docs.pdf` are created; report indicates PDF was generated |
| The same repo and `weasyprint` is not installed | Running `specky export --pdf` | `.specky/export/specky-docs.html` is created; report indicates PDF was skipped and which package to install |
| A doc with a glossary term and a mermaid diagram | Running `specky export` then opening the HTML in a browser | The glossary term appears as a native `<abbr>` with hover tooltip; the mermaid diagram renders as a static SVG inline in the page |
| A doc with a glossary term and a mermaid diagram | Running `specky export --confluence` | The glossary term hover is removed; the mermaid diagram source is wrapped in a `<ac:structured-macro>` code block (no server-side render in Confluence) |
| A doc containing the string `]]>` | Running `specky export --confluence` | The string is split across two CDATA sections so it cannot prematurely end the body |
| A repo with 1 feature doc and 1,500 per-commit docs | Running `specky export` | Only the feature doc is exported and the report says 1,500 were left out — nothing is truncated, the category that grows with commit count is simply excluded |
| Any repo | Running `specky export --pdf --confluence` | The command exits with an argparse error naming the two conflicting flags, and writes nothing |
| A doc whose markdown escaped a `<` inside a fenced block | Running `specky export --confluence` | The CDATA body holds the original `<`, not `&lt;` — a copied snippet has to be the source |
