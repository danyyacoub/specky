---
type: feature
tags: [chat, security, configuration]
related: [chat/local-rag-server]
---

# Chat — Serve Access Control

## What It Does

`specky serve` now serves the rendered viewer as well as the chat endpoint, and who may talk to it is configurable in `specky.toml` under `[serve]`. The defaults are unchanged from before this existed — loopback bind, every origin allowed, no token — so nothing needs configuring for the widget to keep working from a double-clicked `file://` page or from a site served on another port.

## How It Works

1. **One port serves everything** — `GET /` and everything under it come out of `.specky/site/`; `POST /chat` answers questions and `GET /search?q=` answers the viewer's search box. A viewer opened at `http://<host>:<port>/` therefore calls both APIs same-origin, with no CORS involved at all.
2. **The page finds its own API** — a page served over `http(s)` tries its own origin first, because whatever port is serving it is the port most likely to be answering. Only when a route comes back 404, 405 or 501 — meaning some other web server is hosting the files — does it fall back to the chat port baked in at render time, and it then sticks with whichever one answered. This matters because `serve --port 9000` serves the page from a port the rendered asset can't know in advance. A page opened from `file://` has no origin to try, so it goes straight to `http://127.0.0.1:<port>`, the original offline behaviour.
3. **Origin allowlist** — `[serve] allow_origins` defaults to `["*"]`. Narrow it and every request carrying a disallowed `Origin` gets 403 with no CORS headers, so the calling page can't read the rejection either. An allowed origin is echoed back rather than `*`, and every response carries `Vary: Origin` so a cache can't hand one origin's response to another.
4. **Optional shared token** — set `[serve] token` and API requests — `/chat` and `/search` alike, since both return doc text — must carry it in an `X-Specky-Token` header. Static files are deliberately not token-gated: a page cannot attach a header to its own `<link>`/`<script>` loads, so gating them would make the served viewer unopenable.
5. **Exposure warning** — `--host`/`[serve] host` can bind anywhere, and `serve()` prints a warning naming what is exposed whenever the bind address isn't a loopback address.

## What the open default costs

With `allow_origins = ["*"]` and no token, any page open in a reader's browser can POST to the port and read answers derived from this repo's docs. Bound to `127.0.0.1` that's limited to software already running on the machine; bound wider it's anyone who can reach the port. The default is open on purpose — it's what makes a site served from any other port work untouched — and `allow_origins`/`token` are how a repo whose docs aren't for everyone narrows it.

## Outcomes

| Situation | Result |
|---|---|
| No `[serve]` table | `127.0.0.1:8420`, all origins, no token — as before |
| Viewer opened at the server's own port | Chat and search are same-origin; no preflight, no CORS headers needed |
| Viewer served on a port other than the rendered default | Still works: the page tries its own origin first |
| Viewer served by some unrelated web server | Its API calls fall back to the chat port and stay there |
| Viewer opened from `file://` | Chat reaches `127.0.0.1:<port>` cross-origin, allowed by default |
| `GET /search` with no `q` | 400 saying `q is required` |
| `GET /search?limit=100000` | Clamped to 100 rows |
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
| `allow_origins` narrowed and a token set | GET `/search?q=refund` from a disallowed origin, then an allowed one without the token, then with it | 403, 403, 200 |
| An indexed repo | GET `/search?q=` a term only in a doc's last paragraph | The doc is returned, ranked, with a snippet |
| A rendered site | GET `/` | The site's `index.html` |
| A rendered site | GET `/../secret.txt` or `/%2e%2e/secret.txt` | 404, file contents never sent |
| No `.specky/site/` | GET `/` | 404 telling you to run `specky render-html` |
