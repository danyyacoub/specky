---
type: feature
tags: [chat, ai, security]
---

# Chat — Mcp Http Transport

## What It Does

`specky serve` already publishes a repo's docs, index and history behind its login. This change adds `/mcp` to that server, answering the same MCP tools (`search_docs`, `read_doc`, `get_graph` and the rest) that the stdio `specky-mcp` exposes. A remote agent can then point at `https://<server>/mcp` with a Basic `Authorization` header and use the repo's docs as a knowledge graph — no checkout, no local install.

Most agents add a remote MCP server from a URL alone and can't attach a Basic auth header that way, so the logged-in viewer also hands out a **personal MCP URL**, `/mcp/k/<key>`, derived from the server's password and token. The viewer surfaces it on a **Connect an agent** page (`connect.html`, linked from the titlebar as "Connect an agent" with a plug icon), which offers one command or install button per agent. `GET /mcp/connect` returns that key to the logged-in page as a path (not an absolute URL — only the page knows the origin it was reached at, proxy and all).

A deployed server has the docs but neither the code nor the repo's real git history — the image is the docs tree copied into a fresh `git init` — so the tools that make the URL MCP a knowledge graph read everything from the docs, the history docs included:

- **`doc_context(topic)`** — one call for a doc and what surrounds it: a doc path, or the top `search_docs` hit for a few words (`alternatives` names the runners-up). It returns the doc's frontmatter fields, content, behaviours by id, its `neighbours` (docs linked by `related:` either way or by a shared tag, each with the reason) and its `recent` changes.
- **`module_acceptance_tests(module, include_edge_cases)`** — every `AT-n` row (and `EDGE-n` when asked) the docs of one module state, grouped by doc. A module is a domain (a folder under the docs root) or one doc path.
- **`search_history(query, author, module, since, impact, limit)`** — the history docs filtered by full text, author (part of a name or email), module, a window (`90m`, `24h`, `7d`, `2w` or an ISO date) and impact. Read from the history docs' own Date, Author, `impact:` and `features:`, never from `git log`.
- **`recent_activity(since, module)`** — the same history summed up by module, with counts per author and per impact.
- **Resources** — `specky://product`, `specky://modules`, `specky://glossary` (PRODUCT.md, MODULES.md, GLOSSARY.md) and `specky://doc/<domain>/<topic>.md`, for hosts that attach resources rather than call tools.

`commits_for_doc` falls back to the history docs naming a doc when the index links no commits to it, which is the deployed case.

## How It Works

1. **Route matched** — a request whose path is `/mcp` is recognised by the `serve` handler before the `/chat`, `/draft` and `/search` routes.
2. **Auth gate first** — the request must already have passed the same Basic login, origin allowlist and token checks as `/chat`; MCP itself does no auth.
3. **Bridge started lazily** — on the first `/mcp` request, `McpBridge` starts the MCP SDK's streamable-HTTP app, pins the server's repo root, and runs its event loop in a background thread. A viewer-only deployment never pays for this.
4. **Request handed over** — the handler reads the body and passes method, path, query, headers and body to `mcp_bridge.handle` on that event loop. `Authorization` is stripped from the headers first: never hand the key on to the SDK — a personal URL is `/mcp` once it has been checked, and the path forwarded to the bridge is always `MCP_PATH`.
5. **SDK answers statelessly** — the app is mounted with JSON responses and stateless HTTP: each POST is answered in a single response body, no session kept between requests.
6. **Response relayed** — the handler sends the SDK's status and headers (dropping the app's `Content-Length`, `Transfer-Encoding` and `Access-Control-*`), adds its own `Content-Length` and the `serve` CORS headers, then writes the body whole.
7. **Banner updated** — on startup `serve` now prints the MCP URL alongside the viewer and chat URLs.
8. **Personal key route** — `/mcp/k/<key>` is checked by `_keyed_mcp` before the ordinary auth gate on both GET and POST: if the origin is not allowed it is refused; if the key does not verify it is a plain `404 {"error": "not found"}` with no Basic challenge (which would only make an agent prompt for a password it was never meant to need); if it verifies, the request is handled as `/mcp` (`_mcp`).
9. **Connect page** — a logged-in `GET /mcp/connect` (after `_authenticated` and `_origin_ok`, gated by `_api_allowed`) returns `{"path": "/mcp/k/<key>"}`, or `{"path": "/mcp"}` when there is nothing to stand in for. The titlebar link and `connect.html` render that path per agent.
10. **History read from the docs** — `search_history`, `recent_activity` and `doc_context`'s `recent` walk the history directory, read each doc's headline, What changed and Why (`commit_doc.read_history`) and its Date and Author bullets (`commit_doc.doc_stamp`), and keep the docs matching every filter given. An entry's date is a span; its end counts. A `query` ranks the history docs through the docs index first.
11. **Module resolved** — `module_acceptance_tests` looks the domain up in `list_domains` and runs `doc_behaviours` on each doc in it; an unknown module is an error listing the real ones.
12. **Bad input reported** — a bad `since`, path or module is a `ValueError` the tool turns into a `ToolError`, so the agent's model reads why.

## Outcomes

| Outcome | When |
|---|---|
| MCP response relayed | `/mcp` request passes login, origin and token checks; SDK returns a result |
| MCP response relayed (personal URL) | `/mcp/k/<key>` request passes the origin check and the key verifies |
| `500 {"error": …}` | `mcp_bridge.handle` raises; the traceback is printed to stderr |
| `401`/`403` (unauth) or CORS refusal | Fails the handler's `_authenticated`/`_api_allowed`/`_origin_ok` checks, unchanged from `/chat` |
| `404 {"error": "not found"}` | POST to a path that is neither `/mcp` nor a known chat route; or `/mcp/k/<key>` with a wrong key (no Basic challenge) |
| `200 {"path": …}` | Logged-in `GET /mcp/connect` passes `_api_allowed`; path is `/mcp/k/<key>` or `/mcp` |
| MCP URL printed at startup | Always, in the `specky serve:` banner line |
| History answered without git | `search_history` / `recent_activity` / `commits_for_doc` on a checkout whose git doesn't hold the history docs' commits |
| Tool error naming the accepted forms | `since` is neither a duration nor an ISO date, or the module doesn't exist |

## Constants & Invariants

- `MCP_PATH = "/mcp"` — the only path forwarded to the bridge; the SDK is always handed `MCP_PATH`.
- `HISTORY_LIMIT = 10` — `search_history`'s default number of changes; any limit is clamped to `SEARCH_LIMIT_MAX = 25`.
- `CONTEXT_NEIGHBOURS = 12`, `CONTEXT_RECENT = 5` — how many neighbours and recent changes `doc_context` returns.
- `since` accepts `<n>m|min|h|d|w` or an ISO date/datetime; one with no offset is UTC. Dates are compared as aware datetimes, never as strings.
- `search_history` with no query and no filter returns nothing rather than the whole history.
- `CALL_TIMEOUT = 60.0` seconds — longest a single MCP call may take before the handler gives up.
- `MCP_CONNECT_PATH = f"{MCP_PATH}/connect"` = `/mcp/connect` — the logged-in page's key lookup.
- `MCP_KEY_PREFIX = f"{MCP_PATH}/k/"` = `/mcp/k/` — the personal MCP URL prefix.
- `CONNECTION_KEY_CHARS = 32` — a 32-character base64url prefix of the HMAC digest: 192 bits, unguessable and short enough to paste.
- Mount options are fixed: `json_response=True`, `stateless_http=True`, `streamable_http_path=MCP_PATH`.
- Response headers from the SDK are filtered: `content-length` and `transfer-encoding` (lowercased) are dropped, and any header starting `access-control-` is dropped; CORS is the handler's call.
- `Authorization` is stripped from the headers passed to `mcp_bridge.handle` (case-insensitive).
- Added CORS-allowed headers on `/chat`-style responses: `Content-Type`, the token header, `Authorization`, `Mcp-Protocol-Version`, `Mcp-Session-Id`.
- `logging.getLogger("mcp")` is forced to `WARNING` so stateless-session INFO lines don't appear in `serve`'s quiet access log.
- Auth precedence: handler checks (`_authenticated`, then `_api_allowed`/`_origin_ok`) run **before** the request reaches `McpBridge`; the SDK's own auth is not used. The keyed route (`/mcp/k/<key>`) runs before those checks, substituting the key for the login **and** the token; a bad key is a plain `404`, never a `401` challenge.

## Maintainer Notes

- The tools and prompts come from `specky.mcp_server`'s `MCPServer` (its own `mcp` object); the HTTP bridge wraps that same server so the stdio and HTTP transports cannot drift. Changes to `MCPServer` tools must not need a matching edit here.
- `McpBridge._start` calls `mcp_server.pin_repo(self._repo_root)` — the served repo is pinned so no server-to-client channel (`roots/list`, sampling) is needed. If a future tool needs that channel, stateless HTTP will not provide it.
- Because the mount is stateless with JSON responses, there is no session to keep alive between calls; `CALL_TIMEOUT` bounds each single call instead.
- The DNS-rebinding guard is being disabled (`TransportSecuritySettings`) so a deployed hostname isn't refused by the SDK's localhost-only Host/Origin check.
- Any new cross-origin MCP headers must be added to the `Access-Control-Allow-Headers` list in `_cors`.
- `ServeConfig.connection_key()` is **derived, not stored**: an HMAC-SHA256 of `f"specky-mcp\n{username}"` keyed by `f"{password}\n{token}"`, base64url-encoded and truncated to `CONNECTION_KEY_CHARS`. Nothing has to be kept anywhere for it to verify, and changing either the password or the token revokes every URL ever handed out — the same thing that locks the browser out locks the agents out. It returns `""` when `auth_required` is false **and** no token is set, meaning there is nothing for the URL to stand in for; `connection_key_ok` then always fails.
- `/mcp/k/<key>` still passes through `_origin_ok`, so a stolen key is only usable from an allowed origin; a wrong key is a `404` rather than a `401` so agents don't prompt for a password.
- `GET /mcp/connect` returns a **path**, not a URL — the page supplies the origin it was reached at, so proxies and hostnames don't have to be known server-side.

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
| An agent that can only be added by URL | It is given `/mcp/k/<key>` from the Connect page | A POST there passes the origin check and key check and is handled as `/mcp` |
| `/mcp/k/<key>` with a wrong or missing key | The request arrives from an allowed origin | `404 {"error": "not found"}` with no Basic challenge, and `routed_to_mcp` reflects the refusal |
| A logged-in viewer page | It requests `GET /mcp/connect` | It receives `{"path": "/mcp/k/<key>"}`, or `{"path": "/mcp"}` when no key exists |
| `Authorization` is present on an `/mcp` request | The bridge is called | The header is stripped before `mcp_bridge.handle` sees it, and the forwarded path is `MCP_PATH` |
| A served repo whose git is one synthetic commit, with history docs | `search_history(author="ann", since="24h")` is called over `/mcp` | Ann's changes from the last day come back, read from the history docs |
| History docs dated `11:00+02:00` and `10:00+00:00` on the same day | `search_history(since="24h")` | Both are compared in UTC; neither is misjudged by its offset |
| A module `billing` with two docs, one stating no acceptance tests | `module_acceptance_tests("billing")` | Only the doc with tests is listed, each test with its `AT-n` id and fields |
| `module_acceptance_tests("payments")` where no such domain exists | The tool is called | A tool error listing the modules there are |
| A doc with a `related:` link and a tag it shares | `doc_context` on it | Both docs are in `neighbours`, with `related` and `tag: <name>` as the reasons |
| `doc_context("panel docked")` | The words match a doc | The top search hit is returned, and the next hits are in `alternatives` |
| A host reading resources | It reads `specky://doc/chat/panel.md` | The doc's text, through the same docs-tree containment check as `read_doc` |
