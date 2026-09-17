---
sha: cefab4bf03d0f0bf7896a59aff83fad45b3a8f26
---

# Commit cefab4bf

- **Date:** 2026-09-17T10:39:39+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Message:** docs: document the feature/workflow graph

Added `specs/catalog/feature-graph.md` documenting the feature/workflow graph — the `specky graph`, `features`, `workflows`, and `tags` commands plus their MCP counterparts. Previously these were only mentioned in passing in the classification doc, which got the tag directionality rule and commit display wrong; this new doc corrects that by covering the graph's tag-edge rules, de-duplication behavior, and `related:` resolution semantics.
