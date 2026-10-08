---
type: feature
tags: [chat, ai, security]
---

# Chat — Mcp Http Transport

## What It Does
`specky serve` already publishes a repo's docs, index and history behind its login. This change adds `/mcp` to that server, answering the same MCP tools (`search_docs`, `read_doc`, `get_graph` and the rest) that the stdio `specky-mcp` exposes. A remote agent can then point at `https://<server>/mcp` with a Basic `Authorization` header and use the repo's docs as a knowledge graph — no checkout, no local install.

## How It Works
1. **Route matched** — a request whose path is `/mcp` is recognised by the `serve` handler before the `/chat`, `/draft` and `/search` routes.
2. **Auth gate first** — the request must already have passed the same Basic login, origin allowlist and token checks as `/chat`; MCP itself does no auth.
3. **Bridge started lazily** — on the first `/mcp` request, `McpBridge` starts the MCP SDK's streamable-HTTP app, pins the server's repo root, and runs its event loop in a background thread. A viewer-only deployment never pays for this.
4. **Request handed over** — the handler reads the body and passes method, path, query, headers and body to `mcp_bridge.handle` on that event loop.
5. **SDK answers statelessly** — the app is mounted with JSON responses and stateless HTTP: each POST is answered in a single response body, no session kept between requests.
6. **Response relayed** — the handler sends the SDK's status and headers (dropping the app's `Content-Length`, `Transfer-Encoding` and `Access-Control-*`), adds its own `Content-Length` and the `serve` CORS headers, then writes the body whole.
7. **Banner updated** — on startup `serve` now prints the MCP URL alongside the viewer and chat URLs.

## Outcomes
| Outcome | When |
|---|---|
| MCP response relayed | `/mcp` request passes login, origin and token checks; SDK returns a result |
| `500 {"error": …}` | `mcp_bridge.handle` raises; the traceback is printed to stderr |
| `401`/`403` (unauth) or CORS refusal | Fails the handler's `_authenticated`/`_api_allowed`/`_origin_ok` checks, unchanged from `/chat` |
| `404 {"error": "not found"}` | POST to a path that is neither `/mcp` nor a known chat route |
| MCP URL printed at startup | Always, in the `specky serve:` banner line |

## Constants & Invariants
- `MCP_PATH = "/mcp"` — the only path that routes to the bridge.
- `CALL_TIMEOUT = 60.0` seconds — longest a single MCP call may take before the handler gives up.
- Mount options are fixed: `json_response=True`, `stateless_http=True`, `streamable_http_path=MCP_PATH`.
- Response headers from the SDK are filtered: `content-length` and `transfer-encoding` (lowercased) are dropped, and any header starting `access-control-` is dropped; CORS is the handler's call.
- Added CORS-allowed headers on `/chat`-style responses: `Content-Type`, the token header, `Authorization`, `Mcp-Protocol-Version`, `Mcp-Session-Id`.
- `logging.getLogger("mcp")` is forced to `WARNING` so stateless-session INFO lines don't appear in `serve`'s quiet access log.
- Auth precedence: handler checks (`_authenticated`, then `_api_allowed`/`_origin_ok`) run **before** the request reaches `McpBridge`; the SDK's own auth is not used.

## Maintainer Notes
- The tools and prompts come from `specky.mcp_server`'s `MCPServer` (its own `mcp` object); the HTTP bridge wraps that same server so the stdio and HTTP transports cannot drift. Changes to `MCPServer` tools must not need a matching edit here.
- `McpBridge._start` calls `mcp_server.pin_repo(self._repo_root)` — the served repo is pinned so no server-to-client channel (`roots/list`, sampling) is needed. If a future tool needs that channel, stateless HTTP will not provide it.
- Because the mount is stateless with JSON responses, there is no session to keep alive between calls; `CALL_TIMEOUT` bounds each single call instead.
- The DNS-rebinding guard is being disabled (`TransportSecuritySettings`) so a deployed hostname isn't refused by the SDK's localhost-only Host/Origin check.
- Any new cross-origin MCP headers must be added to the `Access-Control-Allow-Headers` list in `_cors`.

## Acceptance Tests

```matrix
inputs: path (text), method (enum[GET, POST]), authenticated (bool), api_allowed (bool)
expect: routed_to_mcp (bool), status (enum[relayed, 401/403, 404, 500])
---
path=/mcp, method=POST, authenticated=true, api_allowed=true  → routed_to_mcp=true,  status=relayed
path=/mcp, method=GET,  authenticated=true, api_allowed=true  → routed_to_mcp=true,  status=relayed
path=/mcp, method=POST, authenticated=false, api_allowed=true → routed_to_mcp=false, status=401/403
path=/mcp, method=POST, authenticated=true, api_allowed=false → routed_to_mcp=false, status=401/403
path=/chat,   method=POST, authenticated=true, api_allowed=true  → routed_to_mcp=false, status=relayed
path=/other,  method=POST, authenticated=true, api_allowed=true  → routed_to_mcp=false, status=404
```

Given/When/Then cases:

| Given | When | Then |
|---|---|---|
| A running `specky serve` with the repo pinned | Any `/mcp` POST arrives after auth | The SDK answers in one JSON body; the response carries `serve`'s CORS headers and a correct `Content-Length` |
| The MCP bridge raises during handling | A `/mcp` request is processed | The handler returns `500 {"error": …}` and prints the traceback to stderr |
| A browser MCP client (e.g. the Inspector) calls `/mcp` cross-origin | Preflight/response headers are inspected | `Authorization`, `Mcp-Protocol-Version` and `Mcp-Session-Id` are allowed by CORS |
| A deployment that never receives `/mcp` traffic | The server runs as viewer-only | No MCP event loop thread is started and the MCP server is not imported |
| Startup banner is printed | `serve` starts | The line lists viewer, `/chat` and the MCP URL (`/mcp`) |
| An MCP call runs longer than `CALL_TIMEOUT` | The handler waits | The handler gives up on the call |
