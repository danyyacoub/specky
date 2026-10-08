---
type: feature
tags: [rendering, documentation]
related: [rendering/home-activity-brief, documentation/auto-commit-docs, chat/mcp-http-transport]
sources: [src/specky/changelog.py, src/specky/html_render.py, src/specky/doc_tools.py, src/specky/mcp_server.py]
---

# Rendering — Changelog

## What It Does

The viewer has a **Changelog**: what changed in the product, one entry per day, newest first. It's
for a reader who wants to know what's different since they last looked, without reading commits or
caring how many times the team deployed.

- **A day is the unit, not a deploy.** Deploys can be frequent and say nothing, so everything
  user-facing that changed on a day sits under that day's date. A day with only `internal` work
  doesn't appear at all.
- **Releases are markers.** A release tag cut on a day shows as a small chip next to that day's
  date (`v0.3.1`). It's never an entry of its own, so a release that changed nothing adds nothing.
- **Behaviour first.** Within a day, `breaking` changes come first, then `feature`, `improvement`
  and `fix`, each newest first.
- **Each change shows** its impact, its headline linked to its history page, its What changed
  paragraph, its Example (a scenario, what a user saw before and sees now), the feature docs it's
  about, and who made it.

It appears in four places:

| Where | What |
|---|---|
| `changelog.html`, linked from the titlebar of every page | The last `[changelog] days` (90) |
| The home page, above Recent activity | The last `[changelog] home_days` (7), with a "Full changelog →" link |
| The viewer's search box | One result per day, matched on its headlines, What changed and Example text, opening that day on the changelog page |
| MCP `recent_activity` | A `days` list for the window: each day's date, release tags, and changes with their impact, headline, what, example, docs and authors |

No model is called to build it; it's the committed history docs, plus the repo's tags.

A `breaking` impact marks changes users must adapt to, and within a day it is listed first.

## How It Works

1. **Read the history docs.** Every doc under `<docs root>/history/` gives its date (the end of
   its Date span), its authors, its impact, its `features:` and its words (headline, What changed,
   Example). Git isn't walked, so
   the changelog is the same on a laptop and on a deployed server, whose checkout is the docs copied
   into a fresh `git init`.
2. **Keep what's user-facing.** Docs older than the window, and docs whose impact is `internal`,
   are dropped.
3. **Group by day.** A day is the doc's date in the doc's own time zone: the day its author saw it
   happen.
4. **Mark releases.** The tags matching `[changelog] tag_pattern` (`v*`) are read with one `git
   for-each-ref`, and each lands on the day it was created. A checkout without the tags, or without
   git, simply has no markers.
5. **Order.** Days newest first. Within a day, by impact (`breaking`, `feature`, `improvement`,
   `fix`, unclassified), then newest first. Past 5, a day's list ends in "+N more".

## Configuration

`[changelog]` in `specky.toml`, or `[tool.specky.changelog]` in `pyproject.toml`:

| Key | Default | Meaning |
|---|---|---|
| `days` | `90` | How far back `changelog.html` reaches |
| `home_days` | `7` | How many days the home page shows |
| `tag_pattern` | `"v*"` | fnmatch pattern for the tags shown as release markers |
| `enabled` | `true` | `false` leaves out the page, the home block and the search entries |

## Outcomes

| Condition | Result |
|---|---|
| Two changes today and one two days ago | Two days, today first |
| A `breaking` change and a `feature` on the same day | The breaking change is listed first, with a red badge |
| A day whose only history doc is `impact: internal` | The day doesn't appear |
| A `v1.2.0` tag and a `nightly-42` tag cut today, with the default pattern | Today shows a `v1.2.0` marker only |
| A tag cut on a day with no user-facing changes | No day, and so no marker |
| A history doc with an Example section | Its scenario, Before and After show under its What changed |
| A legacy history doc (`# Commit <sha8>`, one paragraph) | Listed by its first sentence, with no What changed paragraph |
| A deployed checkout with no git history | Days from the docs, no release markers |
| A change from 20 days ago | On `changelog.html`, not on the home page |
| `[changelog] enabled = false` | No `changelog.html`, no home block, no changelog search results |
| A breaking change with an example today, and an internal change yesterday | MCP `recent_activity` has one day, today, with the breaking change and its example |

## Acceptance Tests

| Given | When | Then |
|---|---|---|
| History docs dated today (a feature, and a breaking change two hours older) and two days ago (a fix) | The changelog is built for 7 days | Two days, newest first; today's first entry is the breaking change |
| A history doc dated yesterday with `impact: internal`, and a feature from 30 days ago | The changelog is built for 7 days | There are no days; built for 90 days, there is one |
| A history doc dated today, a tag `v1.2.0` and a tag `nightly-42` | The changelog is built | Today's releases are `["v1.2.0"]`, and its only entry is the doc's |
| 15 history docs in the last 15 hours, in a directory that isn't a git repo | The changelog is built | All 15 are listed, and no day has a release marker |
| A history doc with an Example section | The changelog is built | Its entry carries the scenario, before and after |
| `[changelog]` with `days = 30`, `home_days = 3`, `tag_pattern = "release-*"` | The config is loaded | Those values are used |
| A breaking change with an example today and a feature 20 days ago | The site is rendered | `changelog.html` has both, today's anchored by its date, the breaking badge and the example; the home's changelog block has today's change but not the older one, and links to `changelog.html`; the search index has an entry for `changelog.html#<today>` |
| `[changelog] enabled = false` | The site is rendered | There is no `changelog.html`, and the home page has no changelog block |
| A breaking change with an example today, and an internal change yesterday | MCP `recent_activity(since="7d")` is called | `days` has one day, today, with the breaking change and its example |
