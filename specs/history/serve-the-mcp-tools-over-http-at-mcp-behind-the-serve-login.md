---
commits: [985db7ca3d8a2976b6af8e09025752362732bba9, 5929454c3bbb37278a29c29ee78618a97b54f3cc]
branch: main
impact: feature
features: [specs/chat/mcp-http-transport.md]
---

# specky serve hands out a personal MCP link to connect agents

- **Date:** 2026-10-08T13:33:04+02:00 → 2026-10-08T14:07:58+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Commits:**
    - `985db7ca` feat(mcp): serve the MCP tools over HTTP at /mcp behind the serve login
    - `5929454c` feat(viewer): add a Connect an agent page with a personal MCP link

## What changed

The logged-in viewer now has a "Connect an agent" page that shows a personal MCP URL, /mcp/k/<key>, with an install command or button per agent; an agent added by that URL gets the same tools (search_docs, read_doc, get_graph, and the rest) behind the server's login without needing to attach a Basic auth header. The key is derived from the server's password and token, so changing either revokes every URL handed out, and a wrong key answers as not found rather than prompting for a password.

## Why

Most agents add a remote MCP server from a URL alone and can't attach a Basic auth header, so the server needed a way to give remote agents the same access without the standard login.
