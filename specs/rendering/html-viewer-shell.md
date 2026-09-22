---
type: feature
tags: [rendering, search, documentation]
---

# Rendering — Html Viewer Shell

## What It Does

Redesigns the docs viewer from a rail+card layout to a desktop app shell with a titlebar, filterable sidebar, and content pane. Tags and doc types (Feature/Workflow) render as interactive chips that filter the navigation and search results. Related docs appear as a dedicated section on each page, making it easy to navigate between connected topics.

## How It Works

1. **Navigation Structure** — Sidebar organizes docs by domain in collapsible groups, with domain icons and an active indicator on the current page. Each link drops a title prefix that merely repeats its own module (the repo name, for root docs) since the group header already names it, keeping the full title on hover; any other title is left as written. Links and search hits carry the feature or workflow icon in its chip's color so the type reads at a glance.

2. **Tag & Type Filtering** — Colored chips below the domain list let readers filter the sidebar and search by doc type (Feature or Workflow) or by tag name; active filters show a clear button.

3. **Related Links** — Each doc displays a "Related" section rendering the `related:` metadata as real doc-to-doc links instead of raw field values.

4. **Visual Polish** — Titlebar, sidebar, chat panel, and search dropdown use glass/blur effects and semantic color tokens (light/dark variants chosen by system preference). The titlebar and sidebar carry a wash of the accent so the frame reads as color rather than gray, and a doc's section headings, table headers, and blockquotes take its type's color (indigo for a feature, amber for a workflow, accent for unclassified). Icons are a hand-built SVG sprite (no icon font or new dependency). The titlebar and chat panel paint their glass on a layer behind their contents rather than on themselves, so the search dropdown and the @ picker that float out of them blur what lies beneath — the page, or the conversation — instead of letting it read through their translucent fill.

5. **History Collapsing** — Commit history entries collapse by default (a long list of commits would crowd out the reference docs), but auto-expand when active or matched by a filter. Each entry is titled by its history doc's headline; a doc from before headlines existed still reads "Commit <sha8>".

6. **Home Page** — Besides the counts and the tag cloud, the home page carries a Recent activity brief of who changed what (see [rendering/home-activity-brief.md](home-activity-brief.md)).

## Outcomes

| Condition | Result |
|-----------|--------|
| Reader clicks a Feature/Workflow chip | Sidebar and search filter to show only docs of that type; other pages fade visually |
| Reader clicks a tag chip | Sidebar and search filter to show only docs tagged with that value |
| Multiple filters active | Nav and search show intersection of filters; Clear button appears |
| Reader visits a doc with Related entries | Related section appears below main content with linked titles |
| Reader views History section | Collapsed by default; expands on click or when matched by active filters |
| Reader's system is in dark mode | Colors adapt to dark-mode variants (no in-app toggle) |
| Reader searches while scrolled down a doc | Results sit on frosted glass: the page behind them is blurred, never readable through the rows |

## Acceptance Tests

| Given | When | Then |
|-------|------|------|
| Sidebar shows docs with mixed types (Feature, Workflow) and tags | Reader clicks Feature chip | Sidebar hides non-Feature docs and shows only Features; search filtered to match |
| Active filter is set on the page | Reader clicks Clear button | All docs reappear in sidebar and search; Clear button disappears |
| Doc has `related: [other-doc]` in metadata | Page renders normally | "Related" section appears with a link to the other doc |
| History section is displayed with commit-SHA titles | Page loads | History entries appear collapsed; clicking expands to show commit details |
| Active history entry is collapse/expanded when filter is applied | Filter matches an entry | Matched history entry expands automatically even if other history entries stay collapsed |
| Multiple filter chips are active | Reader hovers over a chip | Chip shows active state (visual feedback); count/preview of matching docs updates in real time |
| The content pane is scrolled so body text sits under the search box | Reader types a query | The result rows read cleanly; the text behind the dropdown is blurred, not visible through it, in light and dark mode |
| The chat log is long enough to reach the composer | Reader types `@` in the Spec Assistant | The picker's rows read cleanly over a blurred log |
