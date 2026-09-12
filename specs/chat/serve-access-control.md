---
type: feature
tags: [chat, security, configuration]
related: [chat/local-rag-server]
---

# Chat — Serve Access Control

## What It Does

`specky serve` now serves the rendered viewer as well as the chat endpoint, and who may talk to it
is configurable in `specky.toml` under `[serve]`. The defaults are unchanged from before this
existed — loopback bind, every origin allowed, no token — so nothing needs configuring for the
widget to keep working from a double-clicked `file://` page or from a site served on another port.

## How It Works

1. **One port serves both** — `GET /` and everything under it come out of `.specky/site/`; `POST
   /chat` is the chat endpoint. A viewer opened at `http://<host>:<port>/` therefore calls chat
   same-origin, with no CORS involved at all.
2. **The widget picks its endpoint** — served on the chat server's own port, it uses a relative
   `/chat`; served elsewhere over http(s), it uses the same host with the chat port; opened from
   `file://`, it uses `http://127.0.0.1:<port>`, which is the original offline behaviour.
3. **Origin allowlist** — `[serve] allow_origins` defaults to `["*"]`. Narrow it and every request
   carrying a disallowed `Origin` gets 403 with no CORS headers, so the calling page can't read
   the rejection either. An allowed origin is echoed back rather than `*`, and every response
   carries `Vary: Origin` so a cache can't hand one origin's response to another.
4. **Optional shared token** — set `[serve] token` and API requests must carry it in an
   `X-Specky-Token` header. Static files are deliberately not token-gated: a page cannot attach a
   header to its own `<link>`/`<script>` loads, so gating them would make the served viewer
   unopenable.
5. **Exposure warning** — `--host`/`[serve] host` can bind anywhere, and `serve()` prints a
   warning naming what is exposed whenever the bind address isn't a loopback address.

## What the open default costs

With `allow_origins = ["*"]` and no token, any page open in a reader's browser can POST to the port
and read answers derived from this repo's docs. Bound to `127.0.0.1` that's limited to software
already running on the machine; bound wider it's anyone who can reach the port. The default is open
on purpose — it's what makes a site served from any other port work untouched — and
`allow_origins`/`token` are how a repo whose docs aren't for everyone narrows it.

## Outcomes

| Situation | Result |
|---|---|
| No `[serve]` table | `127.0.0.1:8420`, all origins, no token — as before |
| Viewer opened at the server's own port | Chat is same-origin; no preflight, no CORS headers needed |
| Viewer opened from `file://` | Chat reaches `127.0.0.1:<port>` cross-origin, allowed by default |
| `allow_origins` narrowed, request from elsewhere | 403, no CORS headers, no provider call |
| `token` set, header missing or wrong | 403 on the API; static files still served |
| Request path escaping `.specky/site/` | 404, whatever `../` or percent-encoding it used |
| No site rendered yet | 404 whose message names `specky render-html` |
| `host` not loopback | Serves, and warns once about what is now reachable |

## Acceptance Tests

| Given | When | Then |
|---|---|---|
| Default config | POST `/chat` from any origin | 200, `Access-Control-Allow-Origin: *` |
| `allow_origins = ["https://docs.example"]` | POST from `https://evil.example` | 403, no CORS header, provider not called |
| Same config | POST from `https://docs.example` | 200, origin echoed, `Vary: Origin` |
| `token = "s3cret"` | POST without the header | 403 naming `X-Specky-Token` |
| `token = "s3cret"` | GET a stylesheet without the header | 200 |
| A rendered site | GET `/` | The site's `index.html` |
| A rendered site | GET `/../secret.txt` or `/%2e%2e/secret.txt` | 404, file contents never sent |
| No `.specky/site/` | GET `/` | 404 telling you to run `specky render-html` |
