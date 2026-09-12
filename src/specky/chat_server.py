"""Local RAG-over-FTS5 chat companion for the static HTML viewer.

The generated site is a plain file:// page — it can't safely hold an API key or query
SQLite directly. `specky serve` runs a small local HTTP server instead: the chat widget
POSTs a question to it, it pulls grounding context out of the same FTS5 index `specky
search` uses, and calls the AI provider configured by `specky init`. Browsing and static
search keep working with the server off; the widget just reports that chat is offline.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from specky.ai_provider import load_provider_from_toml
from specky.db import connect, fts_match_query

DEFAULT_PORT = 8420

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
    provider = load_provider_from_toml(repo_root / "specky.toml")
    raw = provider.generate(_build_prompt(question, context))
    answer, html_snippet = _extract_html_snippet(raw)
    result = {"answer": answer, "sources": sorted({c["source"] for c in context})}
    if html_snippet:
        result["html_snippet"] = html_snippet
    return result


def _make_handler(repo_root: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _json(self, status: int, payload: dict) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def do_OPTIONS(self) -> None:  # preflight for the widget's cross-origin fetch()
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_POST(self) -> None:
            if self.path != "/chat":
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

        def log_message(self, format: str, *args) -> None:  # quiet by default
            pass

    return Handler


def serve(repo_root: Path, port: int = DEFAULT_PORT) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(repo_root))
    print(f"specky serve: chat companion listening on http://127.0.0.1:{port} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
