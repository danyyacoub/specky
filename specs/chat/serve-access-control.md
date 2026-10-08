---
type: feature
tags: [chat, security, configuration]
related: [chat/local-rag-server]
---

# Chat — Serve Access Control

## What It Does

`specky serve` now serves the rendered viewer as well as the chat endpoint, and who may talk to it is configurable in `specky.toml` under `[serve]`. The defaults are unchanged from before this existed — loopback bind, every origin allowed, no token — so nothing needs configuring for the widget to keep working from a double-clicked `file://` page or from a site served on another port.

## How It Works

1. **One port serves everything** — `GET /` and everything under it come out of `.specky/site/`; `POST /chat` answers questions, `GET /search?q=` answers the viewer's search box, and `/mcp` answers MCP (see step 7). A viewer opened at `http://<host>:<port>/` therefore calls both APIs same-origin, with no CORS involved at all.
2. **The page finds its own API** — a page served over `http(s)` tries its own origin first, because whatever port is serving it is the port most likely to be answering. Only when a route comes back 404, 405 or 501 — meaning some other web server is hosting the files — does it fall back to the chat port baked in at render time, and it then sticks with whichever one answered. This matters because `serve --port 9000` serves the page from a port the rendered asset can't know in advance. A page opened from `file://` has no origin to try, so it goes straight to `http://127.0.0.1:<port>`, the original offline behaviour.
3. **Origin allowlist** — `[serve] allow_origins` defaults to `["*"]`. Narrow it and every request carrying a disallowed `Origin` gets 403 with no CORS headers, so the calling page can't read the rejection either. An allowed origin is echoed back rather than `*`, and every response carries `Vary: Origin` so a cache can't hand one origin's response to another.
4. **Optional shared token** — set `[serve] token` and API requests — `/chat`, `/search` and `/mcp` alike, since all three return doc text — must carry it in an `X-Specky-Token` header. Static files are deliberately not token-gated: a page cannot attach a header to its own `<link>`/`<script>` loads, so gating them would make the served viewer unopenable.
5. **Login for a deployed server** — set `SPECKY_AUTH_USERNAME` and `SPECKY_AUTH_PASSWORD` in the server's environment and every route — pages, stylesheets, `/chat`, `/search`, `/mcp` — answers 401 with a `WWW-Authenticate: Basic` challenge until the request carries those credentials. The browser shows its own login prompt and then re-sends the credentials on every same-origin load, which is why, unlike the token, this can gate static files. Only CORS preflights skip it, because browsers never attach credentials to one. The credentials live in the environment only, never in `specky.toml`: that file is easily copied into an image or a backup along with the repo, and a password in it would travel with the docs it is meant to protect. Setting only one of the two variables stops `serve` from starting rather than serving the repo open.
6. **Exposure warning** — `--host`/`[serve] host` can bind anywhere. When no login is configured, `serve()` prints a warning naming what is exposed whenever the bind address isn't a loopback address. When a login is configured it says so instead, and reminds you that Basic auth sends the password in the clear, so a deployed server belongs behind HTTPS.
7. **MCP for remote agents** — `/mcp` is a Streamable HTTP MCP endpoint serving the same tools and prompts as the stdio `specky-mcp`: `search_docs`, `read_doc`, `doc_behaviours`, `get_graph`, `commits_for_doc` and the rest. They answer for the repo `serve` was started in, whatever the client's own workspace is. It's stateless and answers each POST with one JSON body, so it needs nothing from the client beyond the request: no session, no `roots/list`. It sits behind everything the API does: the login, `allow_origins`, and `token`. That's what lets an agent on another machine use a deployed server's docs as its knowledge graph with one Basic `Authorization` header (`claude mcp add --transport http … --header "Authorization: Basic …"`).
8. **A personal link for agents that only take a URL** — most agents add a remote MCP server from a URL alone (`codex mcp add … --url`, `devin mcp add`, Cursor's, VS Code's and Kiro's install links), with no way to attach a header. So `/mcp/k/<key>` is `/mcp` with the credential in the path. The key is an HMAC of the username, keyed by the password and `[serve] token`. Nothing is stored, and changing either secret revokes every link handed out. A right key stands in for the login and the token on that one route; the origin allowlist still applies. A wrong key is a plain 404 with no Basic challenge, so an agent never prompts for a password it was never meant to need. `GET /mcp/connect`, behind the normal login, hands the logged-in viewer its link as a path (`/mcp` when the server has no login or token). The viewer's **Connect an agent** page, linked from the titlebar of every page, turns that into one command or install button per agent. The link is built in the browser from the origin the page reached, so it is never baked into the rendered site. Opened without a server, the page says to open it from the deployed one.

## Models on a deployed server

A deployed server usually wants other models than a laptop, and a container built from the repo has no `specky.toml` at all (it's gitignored). So the Spec Assistant's provider can come from the environment too: `SPECKY_AI_PROVIDER` and its keys make the whole `[ai]` table, and `SPECKY_AI_CHAT_MODEL` / `SPECKY_AI_DRAFT_MODEL` route its answers and its drafts to their own models ([provider cost controls](../ai/provider-cost-controls.md)). A server should use an API provider — `bedrock` on AWS needs no key at all, since the container's IAM role is its credential: `provider = "agent"` needs a coding agent logged in on the machine, and it has no tool channel, so drafts lose their code-reading steps.

When the provider fails there, the reader's panel and the server's logs both say why. A request that was itself wrong — no question, a body that isn't JSON, a draft step that can't be taken — is a 400 with words the reader can act on. Anything else failed on the server's side and comes back as a 500 carrying the error. That covers a provider that can't be reached or refuses the call, a search index that was never built, and a broken config. The traceback also goes to stderr, where a container's log driver picks it up, since the access log is kept quiet. An unreachable provider names the URL it was calling and the cause, rather than the SDK's bare "Connection error." ([Bedrock provider](../integration/bedrock-provider.md) for the region case).

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
| POST `/chat` with an empty question | 400 saying `question is required` |
| The provider can't be reached or fails | 500 carrying the error; `specky serve: POST /chat failed` and the traceback on stderr |
| `GET /search?limit=100000` | Clamped to 100 rows |
| `allow_origins` narrowed, request from elsewhere | 403, no CORS headers, no provider call |
| `token` set, header missing or wrong | 403 on the API; static files still served |
| Request path escaping `.specky/site/` | 404, whatever `../` or percent-encoding it used |
| No site rendered yet | 404 whose message names `specky render-html` |
| `host` not loopback, no login | Serves, and warns once about what is now reachable |
| Login env vars set, no credentials sent | 401 with a Basic challenge, on pages and the API alike |
| Login env vars set, correct credentials | Served as if no login were configured; `[serve] token` and `allow_origins` still apply |
| Only one of the two login env vars set | `serve` exits with a message naming both, and never binds |
| Login set, MCP client sends it as Basic auth to `/mcp` | The specky tools, answered from the served repo |
| Login set, `/mcp` without credentials | 401 with a Basic challenge, no tool runs |
| `token` or a narrowed `allow_origins`, `/mcp` without them | 403, as for `/chat` |
| Logged-in viewer opens Connect an agent | A personal `/mcp/k/<key>` link, as a command or install button per agent |
| Agent added with that link | Answered with no `Authorization` header and no token |
| Password or token changed | Every earlier link is a 404 |
| Viewer opened from `file://` with no server running | Connect an agent says to open it from the deployed server |

## Acceptance Tests

| Given | When | Then |
|---|---|---|
| Default config | POST `/chat` from any origin | 200, `Access-Control-Allow-Origin: *` |
| Default config | POST `/chat` with `{"question": "  "}` | 400, `question is required`, provider not called |
| A provider whose endpoint doesn't resolve | POST `/chat` | 500 naming the unreachable URL; the traceback is on stderr |
| `allow_origins = ["https://docs.example"]` | POST from `https://evil.example` | 403, no CORS header, provider not called |
| Same config | POST from `https://docs.example` | 200, origin echoed, `Vary: Origin` |
| `token = "s3cret"` | POST without the header | 403 naming `X-Specky-Token` |
| `token = "s3cret"` | GET a stylesheet without the header | 200 |
| `allow_origins` narrowed and a token set | GET `/search?q=refund` from a disallowed origin, then an allowed one without the token, then with it | 403, 403, 200 |
| An indexed repo | GET `/search?q=` a term only in a doc's last paragraph | The doc is returned, ranked, with a snippet |
| A rendered site | GET `/` | The site's `index.html` |
| A rendered site | GET `/../secret.txt` or `/%2e%2e/secret.txt` | 404, file contents never sent |
| No `.specky/site/` | GET `/` | 404 telling you to run `specky render-html` |
| `SPECKY_AUTH_USERNAME=admin`, `SPECKY_AUTH_PASSWORD=pw` | GET `/`, a stylesheet, `/search`, or POST `/chat` without credentials | 401, `WWW-Authenticate: Basic realm="specky"` |
| Same login | The same requests with `admin:pw` as Basic auth | 200 |
| Same login | Basic auth with a wrong password or username | 401 |
| Same login | OPTIONS preflight without credentials | 204 |
| Login and `token = "s3cret"` | POST `/chat` with the login but no token | 403 |
| Only `SPECKY_AUTH_USERNAME` set | `specky serve` | Exits naming `SPECKY_AUTH_PASSWORD`, nothing bound |
| `SPECKY_AUTH_USERNAME=admin`, `SPECKY_AUTH_PASSWORD=pw` | POST an MCP `initialize` to `/mcp` without credentials, then with `admin:pw` | 401, then 200 from server `specky` |
| Same login, a doc at `specs/billing/refunds.md` | MCP `tools/call` `read_doc` on that path with the login | The doc's text, read from the served repo |
| Login, `token = "s3cret"`, `allow_origins = ["https://docs.example"]` | MCP `initialize` without the token, then from `https://evil.example`, then from `https://docs.example` with it | 403, 403, 200 |
| `SPECKY_AUTH_USERNAME=admin`, `SPECKY_AUTH_PASSWORD=pw` | GET `/mcp/connect` without credentials, then with them | 401, then `{"path": "/mcp/k/<key>"}` |
| Same login and `token = "s3cret"` | MCP `initialize` POSTed to `/mcp/k/<key>` with no credentials and no token | 200 from server `specky` |
| Login changed from `old` to `new` | MCP `initialize` to the key issued under `old`, or to `/mcp/k/nope` | 404, no `WWW-Authenticate` |
| No login and no token | GET `/mcp/connect` | `{"path": "/mcp"}` |
