---
type: workflow
tags: [search, documentation]
---

# Docs — Search And Indexing

## What It Does

Search-and-indexing builds a searchable database of all specs and git history. Users can look up docs from command line, or generate a self-contained static website with built-in search that works offline—no server, no build step, just open `index.html` in a browser.

## How It Works

1. **Build the index** — `specky index` reads all markdown files from `specs/` and git history, then stores searchable content in `.specky/index.db` (a SQLite database optimized for keyword search).
2. **Search from terminal** — `specky search "<query>"` finds matching docs and prints results with titles and text snippets.
3. **Generate static site** — `specky render-html` reads the index and writes a complete website to `.specky/site/index.html` with sidebar navigation grouped by topic, individual doc pages, and a search box.
4. **Search works offline** — The search index is embedded directly into each page (not fetched from a server), so users can double-click the HTML file or open it with `file://` URL and search without any network connection.
5. **Reading and writing can overlap** — The index is a write-ahead-log database, so the post-commit hook can record a new commit's doc while `specky serve` is answering a question and `specky index` is rebuilding, without any of them failing on a locked database. A reader keeps the snapshot it started with until its query finishes, so results are always internally consistent even mid-rebuild.

```mermaid
flowchart TD
    A[specs/**/*.md + git log] --> B[specky index]
    B --> C[.specky/index.db - SQLite FTS5]
    C --> D[specky search 'term']
    C --> E[specky render-html]
    D --> F[Terminal results]
    E --> G[.specky/site/index.html]
    G --> H[Opened via file:// - works offline]
```

## Outcomes

| Scenario | Result |
|----------|--------|
| After running `specky index` | `.specky/index.db` created; terminal shows count of indexed docs and commits |
| After `specky search "term"` | Matching docs listed with title and matching text snippet |
| Search term has no matches | "no matches" message appears |
| After running `specky render-html` | Complete website written to `.specky/site/` |
| Opening website in browser from file manager | Site loads fully functional; no server error; sidebar, pages, and search work |
| A commit lands while `specky serve` is running | Both succeed; neither reports "database is locked" |
| Machine loses power mid-write | Index may be missing the last few commits, never corrupt — `specky index` rebuilds it |

## Acceptance Tests

| Given | When | Then |
|-------|------|------|
| A process holding an open read query on the index | Another process writes a new row and commits | Both succeed; the reader sees its original snapshot until its query ends, then the new row |
| Project with specs/ directory and git history | `specky index` is run | Index file created; success message shows doc and commit counts |
| Index file exists | User runs `specky search "refund"` | Terminal lists matching documents with titles and snippets |
| Index file exists | User runs `specky search "xyzabc123"` (nonexistent term) | "no matches" message; command exits cleanly |
| Index file exists | `specky render-html` is run | Website files appear in `.specky/site/`; main page is `index.html` |
| Website file saved locally | `.specky/site/index.html` opened in browser (no server) | All pages load; sidebar shows topic groupings; search box responds to typing; search results appear instantly |
