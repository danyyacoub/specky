---
commits: [985db7ca3d8a2976b6af8e09025752362732bba9, 5929454c3bbb37278a29c29ee78618a97b54f3cc, 9b0bd5821174dea85069e7a12dfa7bbfebd07df5, 6b29448db6bf5b7058c30e8bc0eaeb4c1e1b7efd]
branch: main
impact: feature
features: [specs/chat/mcp-http-transport.md, specs/chat/mcp-host-guidance.md, specs/rendering/changelog.md]
---

# The changelog groups changes by day and shows before/after examples

- **Date:** 2026-10-08T13:33:04+02:00 → 2026-10-08T16:46:34+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Commits:**
    - `985db7ca` feat(mcp): serve the MCP tools over HTTP at /mcp behind the serve login
    - `5929454c` feat(viewer): add a Connect an agent page with a personal MCP link
    - `9b0bd582` feat(mcp): add knowledge-graph tools that read the docs, not git
    - `6b29448d` feat(rendering): add a day-by-day changelog with before/after examples

## What changed

History entries now carry one concrete example — a scenario with what a user saw before and sees after — and a new `breaking` impact marks changes users must adapt to, listed first. A new day-by-day changelog groups user-facing changes under the day their doc is dated, skips days with only internal work, and shows release tags as markers on the day they were cut; it appears on its own page, as the last week on the home page, in the viewer's search, and as `days` in MCP recent_activity.

## Why

Deploys are frequent and often change nothing a user sees, so a deploy can't be the unit of "what changed"; and a headline and a paragraph didn't show what a change means to a user.
