"""Local RAG-over-FTS5 chat companion for the static HTML viewer, and the viewer's own
optional HTTP server.

The generated site is a plain file:// page — it can't safely hold an API key or query
SQLite directly. `specky serve` runs a small local HTTP server instead: the chat widget
POSTs a question to it, it pulls grounding context out of the same FTS5 index `specky
search` uses, and calls the AI provider configured by `specky init`. Browsing and static
search keep working with the server off; the widget just reports that chat is offline.

The same process also serves `.specky/site/`, so `http://<host>:<port>/` is a working
viewer whose chat calls are same-origin. That's the path a deployed site takes; a
double-clicked `file://` page still works and reaches `/chat` cross-origin.

`GET /search?q=` answers the viewer's search box out of that same index. It exists because
the page can only ship a bounded slice of each doc (see `search_body_cap` in
html_render.py), and that bound tightens as a repo grows — a served page gets exact
whole-body search instead, with nothing downloaded up front.

Access control lives in `[serve]` in specky.toml and defaults to open
(`allow_origins = ["*"]`, no token) so a site served from another port or host keeps
working without configuration. Open means what it says: any page in a reader's browser can
POST to this port and read answers derived from the repo's docs, and a non-loopback `host`
extends that to anyone who can reach the port. `allow_origins` and `token` are how you
narrow it; `serve()` warns when the bind address isn't loopback.
"""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import re
import secrets
import tomllib
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from specky.ai_provider import load_provider_from_toml
from specky.db import connect, fts_match_query

DEFAULT_PORT = 8420
DEFAULT_HOST = "127.0.0.1"

# Header the chat widget sends when `[serve] token` is configured. Not `Authorization`:
# that header triggers a CORS preflight the widget would have to survive anyway, and this
# is a local shared secret, not a bearer credential for a third party.
TOKEN_HEADER = "X-Specky-Token"

# Ceiling on `GET /search?limit=`: the viewer asks for 15, and a caller asking for 100k rows
# would be asking this process to serialize the whole index in one response.
SEARCH_LIMIT_MAX = 100

_SYSTEM_PROMPT = (
    "You are a documentation assistant for this codebase. Answer the user's question "
    "using ONLY the context below — doc excerpts and commit summaries pulled from the "
    "project's specs/ tree and git history. Cite the doc path or commit sha each part "
    "of your answer is drawn from. If the context doesn't contain the answer, say you "
    "couldn't find it in the docs — don't guess or use outside knowledge. If a table, "
    "diagram, or code sample would make the answer clearer, you may add ONE fenced "
    "```html block after your prose with a small, self-contained snippet (inline styles "
    "only, no <script> tags, no external resources) — omit it when plain text is enough."
)

# "#module:<domain>" or "#feature:<slug>" — inserted by the chat widget's mention
# autocomplete (see MENTION_JS in html_render.py), stripped before retrieval/prompting.
_MENTION_RE = re.compile(r"#(module|feature):([\w-]+)")
_HTML_SNIPPET_RE = re.compile(r"```html\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)


def _parse_scope(question: str) -> tuple[str, dict | None]:
    match = _MENTION_RE.search(question)
    scope = {"kind": match.group(1), "value": match.group(2)} if match else None
    clean = _MENTION_RE.sub("", question)
    return " ".join(clean.split()), scope


def _extract_html_snippet(raw: str) -> tuple[str, str | None]:
    match = _HTML_SNIPPET_RE.search(raw)
    if not match:
        return raw.strip(), None
    prose = (raw[: match.start()] + raw[match.end() :]).strip()
    return prose, match.group(1).strip()


def retrieve_context(
    repo_root: Path, question: str, scope: dict | None = None, limit: int = 5
) -> list[dict]:
    match = fts_match_query(question)
    if match is None:
        return []

    conn = connect(repo_root)
    try:
        doc_rows = conn.execute(
            "SELECT path, domain, title, snippet(documents_fts, 3, '', '', '…', 40) "
            "FROM documents_fts WHERE documents_fts MATCH ? ORDER BY rank LIMIT 50",
            (match,),
        ).fetchall()
        commit_rows = conn.execute(
            "SELECT sha, message, snippet(commits_fts, 2, '', '', '…', 40) "
            "FROM commits_fts WHERE commits_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
    finally:
        conn.close()

    if scope is not None:
        if scope["kind"] == "module":
            scoped = [r for r in doc_rows if r[1] == scope["value"]]
        else:
            scoped = [r for r in doc_rows if Path(r[0]).stem == scope["value"]]
        doc_rows = scoped or doc_rows

    context = [
        {"source": path, "label": title, "text": excerpt}
        for path, _domain, title, excerpt in doc_rows[:limit]
    ]
    context += [
        {"source": sha[:8], "label": message, "text": excerpt}
        for sha, message, excerpt in commit_rows
        if excerpt
    ]
    return context


def _build_prompt(question: str, context: list[dict]) -> str:
    blocks = (
        "\n\n".join(f"[{c['source']}] {c['label']}\n{c['text']}" for c in context)
        if context
        else "(no matching docs or commits found in the index)"
    )
    return f"{_SYSTEM_PROMPT}\n\nContext:\n{blocks}\n\nQuestion: {question}\nAnswer:"


def answer_question(repo_root: Path, question: str) -> dict:
    question, scope = _parse_scope(question)
    context = retrieve_context(repo_root, question, scope=scope)
    provider = load_provider_from_toml(repo_root / "specky.toml", "serve")
    raw = provider.generate(_build_prompt(question, context))
    answer, html_snippet = _extract_html_snippet(raw)
    result = {"answer": answer, "sources": sorted({c["source"] for c in context})}
    if html_snippet:
        result["html_snippet"] = html_snippet
    return result


@dataclass(frozen=True)
class ServeConfig:
    """`[serve]` from specky.toml, with CLI flags taking precedence.

    Defaults reproduce the behaviour this server has always had — loopback bind, every
    origin allowed, no token — so an existing repo needs no config change. See the module
    docstring for what "every origin allowed" costs.
    """

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    allow_origins: tuple[str, ...] = ("*",)
    token: str = ""

    @classmethod
    def load(cls, repo_root: Path, host: str | None = None, port: int | None = None) -> ServeConfig:
        table: dict = {}
        config_path = repo_root / "specky.toml"
        if config_path.exists():
            with config_path.open("rb") as f:
                table = tomllib.load(f).get("serve") or {}
        origins = table.get("allow_origins", ["*"])
        return cls(
            host=host or str(table.get("host", DEFAULT_HOST)),
            port=port or int(table.get("port", DEFAULT_PORT)),
            # A single string is what someone writes first (`allow_origins = "null"`); take it.
            allow_origins=tuple([origins] if isinstance(origins, str) else origins),
            token=str(table.get("token", "")),
        )

    @property
    def open_to_everyone(self) -> bool:
        return "*" in self.allow_origins

    def origin_allowed(self, origin: str | None) -> bool:
        """A missing `Origin` is allowed: browsers send it on every cross-origin fetch and on
        `file://` pages (as `null`), so no-header means a non-browser caller (curl, a health
        check) that this allowlist was never protecting against."""
        return self.open_to_everyone or origin is None or origin in self.allow_origins

    def acao_for(self, origin: str | None) -> str | None:
        """The `Access-Control-Allow-Origin` value to echo, or None when the request needs no
        CORS header at all. `*` is only ever sent when the allowlist is literally `*` — echoing
        a specific origin is what keeps a narrowed allowlist meaningful."""
        if self.open_to_everyone:
            return "*"
        return origin

    def token_ok(self, supplied: str | None) -> bool:
        if not self.token:
            return True
        return supplied is not None and secrets.compare_digest(supplied, self.token)


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _make_handler(repo_root: Path, config: ServeConfig) -> type[BaseHTTPRequestHandler]:
    site_dir = (repo_root / ".specky" / "site").resolve()

    class Handler(BaseHTTPRequestHandler):
        def _cors(self) -> None:
            """CORS headers for an already-allowed request. A rejected origin gets none, so a
            disallowed caller can't read the 403 body either."""
            origin = self.headers.get("Origin")
            if not config.origin_allowed(origin):
                return
            acao = config.acao_for(origin)
            if acao:
                self.send_header("Access-Control-Allow-Origin", acao)
            # The response varies by request Origin even when it's currently `*`, so caches
            # (and the browser's own) must not reuse one origin's response for another.
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", f"Content-Type, {TOKEN_HEADER}")

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if config.origin_allowed(origin):
                return True
            self._json(403, {"error": f"origin {origin} is not in [serve] allow_origins"})
            return False

        def _api_allowed(self) -> bool:
            """Origin *and* token. The token guards the API only, never static files — a page
            can't send a header for its own `<link>`/`<script>` loads, so requiring one there
            would make the served viewer unopenable."""
            if not self._origin_ok():
                return False
            if not config.token_ok(self.headers.get(TOKEN_HEADER)):
                self._json(403, {"error": f"missing or invalid {TOKEN_HEADER} header"})
                return False
            return True

        def do_OPTIONS(self) -> None:  # preflight for the file:// widget's cross-origin fetch()
            if not self._origin_ok():
                return
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_POST(self) -> None:
            if not self._api_allowed():
                return
            if urlparse(self.path).path != "/chat":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                question = body.get("question", "").strip()
                if not question:
                    raise ValueError("question is required")
                self._json(200, answer_question(repo_root, question))
            except Exception as exc:
                self._json(400, {"error": str(exc)})

        def _search(self, query: str, limit: int) -> None:
            """The viewer's search box, answered from the FTS5 index instead of the payload the
            page shipped: every doc, whole bodies, ranked — and nothing downloaded up front. See
            `search_body_cap` in html_render.py for what the offline fallback can and can't do."""
            from specky.indexer import search as search_index

            if not query:
                self._json(400, {"error": "q is required"})
                return
            self._json(200, {"results": search_index(repo_root, query, limit=limit)})

        def do_GET(self) -> None:
            """Serve `.specky/site/`, so the viewer and its chat share an origin."""
            if not self._origin_ok():
                return
            route = urlparse(self.path)
            if route.path == "/search":
                if not self._api_allowed():
                    return
                params = parse_qs(route.query)
                try:
                    limit = min(int(params.get("limit", ["10"])[0]), SEARCH_LIMIT_MAX)
                except ValueError:
                    limit = 10
                self._search(params.get("q", [""])[0].strip(), limit)
                return
            rel = unquote(route.path).lstrip("/") or "index.html"
            if rel.endswith("/"):
                rel += "index.html"
            target = (site_dir / rel).resolve()
            # resolve() then containment check: catches `..`, symlinks out of the tree, and
            # absolute paths smuggled in through the URL in one test.
            if not target.is_relative_to(site_dir) or not target.is_file():
                hint = (
                    "no rendered site here yet — run `specky render-html`"
                    if not site_dir.is_dir()
                    else "not found"
                )
                self._json(404, {"error": hint})
                return
            body = target.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            )
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:  # quiet by default
            pass

    return Handler


def serve(repo_root: Path, port: int | None = None, host: str | None = None) -> None:
    config = ServeConfig.load(repo_root, host=host, port=port)
    server = ThreadingHTTPServer((config.host, config.port), _make_handler(repo_root, config))
    url = f"http://{config.host}:{config.port}"
    # flush: stdout is block-buffered when it isn't a terminal, and these two lines have to be
    # visible *before* the server starts blocking in serve_forever — especially the warning.
    print(
        f"specky serve: viewer on {url}/ , chat endpoint on {url}/chat (Ctrl+C to stop)", flush=True
    )
    if not _is_loopback(config.host):
        print(
            f"specky serve: WARNING — bound to {config.host}, which is not loopback. Every doc in "
            "this repo, and AI answers drawn from them, are readable by anyone who can reach this "
            "port. Restrict it with [serve] allow_origins and [serve] token in specky.toml.",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
