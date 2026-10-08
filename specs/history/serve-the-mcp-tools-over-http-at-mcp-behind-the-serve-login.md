---
commits: [985db7ca3d8a2976b6af8e09025752362732bba9, 5929454c3bbb37278a29c29ee78618a97b54f3cc, 9b0bd5821174dea85069e7a12dfa7bbfebd07df5]
branch: main
impact: improvement
features: [specs/chat/mcp-http-transport.md, specs/chat/mcp-host-guidance.md]
---

# MCP agents get the project's history, authors and impact from the docs

- **Date:** 2026-10-08T13:33:04+02:00 → 2026-10-08T15:49:03+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Commits:**
    - `985db7ca` feat(mcp): serve the MCP tools over HTTP at /mcp behind the serve login
    - `5929454c` feat(viewer): add a Connect an agent page with a personal MCP link
    - `9b0bd582` feat(mcp): add knowledge-graph tools that read the docs, not git

## What changed

An agent connected over the personal MCP URL now gets real answers about the project's past even on a deployed specky serve that has only the docs and no usable git history: history search, recent_activity and commits_for_doc read the history docs' own date, authors, impact and features instead of git log, and two new tools, doc_context and module_acceptance_tests, plus the specky:// resources, need nothing but the docs tree.

## Why

A deployed specky serve holds only the docs, copied into a fresh git init, so history searched through git log came back empty over the MCP URL.
