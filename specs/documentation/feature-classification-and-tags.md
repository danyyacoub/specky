---
type: workflow
tags: [documentation, search]
---

---
type: feature
tags: [documentation]
---

# Documentation — Feature Classification And Tags

## What It Does
Spec docs get metadata frontmatter (`type`, `tags`, `related`) that AI-classifies automatically per commit, linking each commit to the docs it affects. Shared tags enable grouping and search across features; optional cross-links connect unrelated docs. All relationships live in SQLite, queryable via CLI (`specky tags`, `specky graph`) and MCP.

## How It Works

1. **Frontmatter structure**: Each spec doc starts with YAML declaring `type` (feature or workflow), `tags` (1-3 kebab-case business/domain concepts, not implementation details), and optionally `related` (hand-authored cross-links to other docs).

2. **Tag vocabulary**: Tags are deliberately shared across docs for the same business concept (e.g., `billing`, `refunds`). Run `specky tags` first and reuse an existing tag if one fits — tags only work for search and grouping if they're consistent across docs.

3. **AI classification**: After each commit, feature sync examines what changed and classifies which documented features/workflows are affected, storing the link in `commit_links` table.

4. **Preserved cross-links**: The `related:` field is hand-authored and preserved across automatic doc regeneration — the AI classifier never overwrites it, so you can manually link two docs that share no tag.

5. **Queryable index**: Frontmatter metadata feeds `specky tags` (list all tags), `specky graph` (show doc relationships and affected commits), tag-based search in `specky search`, and MCP endpoints for features, workflows, tags, and commit info.

## Acceptance Tests

| Given | When | Then |
|-------|------|------|
| New commit touching a documented feature | Feature sync runs after commit | Commit stored in `commit_links` table linked to that feature's tags |
| Existing doc with hand-written `related:` list | Feature sync regenerates the doc body | `related:` field stays unchanged; only content above/below frontmatter updates |
| Multiple docs with tag `billing` | `specky tags` runs | Tag listed once; `specky graph` shows all docs tagged `billing` as connected |
| Two docs sharing no tag | User manually adds `related: [domain/topic]` to frontmatter | `specky graph` shows the cross-link; link persists if doc regenerates later |
| User queries `specky search refunds` | Search runs over FTS5 index | Results include docs tagged `refunds` and commits affecting them |
