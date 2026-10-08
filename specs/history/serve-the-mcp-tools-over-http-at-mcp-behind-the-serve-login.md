---
commits: [985db7ca3d8a2976b6af8e09025752362732bba9]
branch: main
impact: feature
features: [specs/chat/mcp-http-transport.md]
---

# specky serve exposes MCP tools over HTTP at /mcp

- **Date:** 2026-10-08T13:33:04+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Message:** feat(mcp): serve the MCP tools over HTTP at /mcp behind the serve login

## What changed

A running specky serve now answers the same MCP tools (search_docs, read_doc, get_graph, and the rest) at /mcp, behind the existing Basic auth, origin allowlist and token. A remote agent can point at https://<server>/mcp and use the server's docs, index and git history as a knowledge graph, with no local checkout or specky-mcp install. The startup banner now prints the MCP URL alongside the viewer and Spec Assistant.

## Why

A deployed specky serve already holds the docs, index and history behind a login, so serving the same tools from it lets a remote agent use a repo's docs as a knowledge graph without a checkout or local install.
