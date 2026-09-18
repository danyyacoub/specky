"""Local RAG-over-FTS5 chat companion for the static HTML viewer, and the viewer's own
optional HTTP server.

The generated site is a plain file:// page — it can't safely hold an API key or query
SQLite directly. `specky serve` runs a small local HTTP server instead: the chat widget
POSTs a question to it, it uses the same FTS5 index `specky search` uses to work out
*which* docs the question is about, sends those docs to the AI provider configured by
`specky init` in full, and returns the grounded answer. Browsing and static search keep
working with the server off; the widget just reports that chat is offline.

The same process also serves `.specky/site/`, so `http://<host>:<port>/` is a working
viewer whose chat calls are same-origin. That's the path a deployed site takes; a
double-clicked `file://` page still works and reaches `/chat` cross-origin.

`GET /search?q=` answers the viewer's search box out of that same index. It exists because
the page can only ship a bounded slice of each doc (see `search_body_cap` in
html_render.py), and that bound tightens as a repo grows — a served page gets exact
whole-body search instead, with nothing downloaded up front.

A `session` on a `/chat` request buys follow-up questions: the last few turns of that
session are replayed to the provider so "why?" and "what about the other one?" mean
something. The transcript lives in this process's memory and in nothing else — see
`ConversationStore`.

Two questions in the same panel can want two different things, so the prompt is steered by
intent: `INTENT_EXPLORE` explains what the docs say, `INTENT_SPEC` drafts a new doc in the
project's own convention. `classify_intent()` guesses from the question's phrasing and the
widget's chips can pin it (`intent` on the request). Either way the answer comes back as both
the markdown the model wrote and that markdown rendered for the panel — tables, headings and
```mermaid``` diagrams included, sanitized in `answer_render`.

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
import threading
import tomllib
from collections import OrderedDict
from collections.abc import Sequence
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

# Turns of a conversation replayed to the provider. Enough for a chain of follow-ups, short
# enough that the reader isn't paying for their whole afternoon on every question.
HISTORY_TURNS = 6
# Characters kept per question and per answer. Every stored turn is re-sent on every later
# question in that conversation, so this bounds prompt cost as much as it bounds memory.
HISTORY_CHARS = 800
# Conversations held at once, least recently used dropped first. A session id is whatever the
# caller made up, so without a cap this dict grows for as long as the process runs.
MAX_SESSIONS = 64
# Longer than any id the widget generates (a UUID is 36 characters). Past this, treat the id as
# junk and answer statelessly rather than storing a key of arbitrary size.
SESSION_ID_MAX = 64

# FTS5 decides WHICH docs a question is about; the provider then gets those docs WHOLE. The
# index's `snippet()` output is ~40 tokens around one match — enough to rank a doc, nowhere
# near enough to answer from it, and the widget's own worst answers were the honest
# consequence: "the docs mention payment settlement but its contents are not in the context".
# A doc is the unit a specky answer is grounded in, so a doc is what gets sent.
#
# Total characters of doc bodies in one prompt. ~15k tokens: large enough for the handful of
# docs a question spans, small enough that a reader asking ten questions isn't paying for the
# whole specs/ tree ten times.
CONTEXT_CHARS_MAX = 60_000
# Ranked hits taken from the index per question. Docs get more room than commits because the docs
# are the answer and the commits are the corroboration — and because the character budget, not
# this count, is what bounds the prompt: a hit past the budget costs an excerpt, not a whole doc.
# Being generous here is how the doc that actually answers the question survives a near-tie in
# the ranking, which is the normal case on a small specs/ tree.
DOC_HITS = 8
COMMIT_HITS = 5
# One doc's share of that budget, so a single outsized doc can't crowd out the others that
# would have answered the question. Past it the body is truncated, and said to be.
DOC_CHARS_MAX = 24_000
# A commit's micro-doc summary is already a summary, so it's sent whole up to this bound
# rather than excerpted — but it's a paragraph, not a doc, and gets a paragraph's budget.
COMMIT_CHARS_MAX = 2_000

_GROUNDING_RULES = (
    "You are a documentation assistant for this codebase. Work from ONLY the context below — "
    "whole docs from the project's specs/ tree, plus commit summaries from its git history. Cite "
    "the doc path or commit sha each part of your answer is drawn from. A block marked as an "
    "excerpt is a search hit from a doc that was too long to include in full: name that doc as "
    "worth reading rather than answering from the fragment. If the context doesn't contain what "
    "you need, say so — don't guess or use outside knowledge."
)

# The two registers a question can be in, and the reason this file has prompts at all rather than
# one prompt. A reader asking "how does X work" wants the docs explained; a reader asking for a
# spec wants a doc drafted. The same instructions can't serve both — asked to draft, the discovery
# prompt answers *about* the docs instead of writing one.
INTENT_EXPLORE = "explore"
INTENT_SPEC = "spec"
INTENTS = (INTENT_EXPLORE, INTENT_SPEC)

# Diagram types the viewer can actually draw — `beautiful-mermaid` via html_render.render_mermaid_svg.
# Named in the prompt because a diagram in a type it can't render degrades to fenced source text,
# which is a worse answer than no diagram at all.
_MERMAID_TYPES = (
    "`graph TD` / `flowchart LR`, `sequenceDiagram`, `stateDiagram-v2`, `erDiagram`, "
    "`classDiagram`, `pie`, `xychart-beta` (bar and line charts)"
)

_EXPLORE_INSTRUCTIONS = (
    "Answer the reader's question. Write markdown: lead with the direct answer in one or two "
    "sentences, then the detail. Use a table when you're comparing cases, statuses or options, "
    "and short headings when the answer has parts. Include at most ONE fenced ```mermaid block, "
    "and only when a flow, a relationship or a quantity is the actual point of the answer — "
    f"supported types are {_MERMAID_TYPES}. Never emit raw HTML."
)

# The doc shape here is `generator.DOC_STYLE_INSTRUCTIONS`'s, deliberately: a draft a reader asks
# for in the panel and a doc specky generates from a commit should be the same kind of document,
# or the draft is a second convention nobody asked for.
_SPEC_INSTRUCTIONS = (
    "The reader is drafting documentation, not asking what the docs already say. Write the draft, "
    "in markdown, in this project's own doc convention:\n"
    "- First line: the path it should live at, as `specs/<domain>/<topic>.md` — kebab-case topic, "
    "an existing domain where one fits.\n"
    "- If any doc in the context already covers this subject, say so first and name it: a second "
    "doc on the same subject is a defect, and updating that one is the right move.\n"
    "- Then the doc itself: `# Domain — Topic`, `## What It Does` (2-3 plain-language sentences), "
    "`## How It Works` (numbered steps, one sentence each, bold label per step), `## Outcomes` "
    "(a table of the distinct outcomes or statuses), `## Acceptance Tests` (a Given/When/Then "
    "table).\n"
    "- If the subject is a workflow rather than a feature — a multi-step process with a sequence "
    "and an outcome per step — `## How It Works` carries the happy path and nothing else, a "
    "```mermaid block of those same steps goes directly under them, and a `## Edge Cases` table "
    "(Situation | What happens | Why) goes before the Acceptance Tests, which then cover every "
    "row of it.\n"
    "- Anything the context doesn't settle is written as `TBD — not in the docs` in place, never "
    "invented and never quietly left out.\n"
    "- A diagram is welcome where a flow needs one: at most ONE fenced ```mermaid block, "
    f"supported types are {_MERMAID_TYPES}. Never emit raw HTML.\n"
    "Style: plain language, compact, WHAT and WHY over implementation detail, tables for "
    "structured information, readable by a non-technical stakeholder."
)

_INTENT_INSTRUCTIONS = {
    INTENT_EXPLORE: _EXPLORE_INSTRUCTIONS,
    INTENT_SPEC: _SPEC_INSTRUCTIONS,
}

# Spec intent is decided on phrases, not words. Every single word that suggests drafting is also
# ordinary discovery vocabulary — "where is the retry added", "what does this feature do", "which
# docs describe the plan" — so a word-level rule fires on questions that wanted an explanation and
# gets a half-invented doc instead. A verb next to its object is the signal; the default is
# discovery, which is both the common case and the cheaper thing to get wrong.
_SPEC_INTENT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(write|draft|create|author|outline|propose|design|sketch|generate|start)\b[\w\s,'-]{0,30}"
        r"\b(spec|specs|doc|docs|documentation|feature doc|user story|story|requirements|"
        r"acceptance|epic|rfc|prd)\b",
        # "for the retry path" is what makes this a request for criteria; without it the phrase is
        # as likely to be a question *about* the section every generated doc already has.
        r"\bacceptance (tests?|criteria) (for|of|around)\b",
        r"\brequirements (for|of|around)\b",
        r"\bhow should (we|i|it|this)\b",
        r"\bwe (need|want|should) to\b",
        r"\b(should|must) support\b",
        r"\bnew (feature|workflow|doc|spec) for\b",
        r"\bspec (out|for)\b",
        r"\bdocument (a|an|the) new\b",
    )
)

# "#module:<domain>" or "#feature:<slug>" — inserted by the chat widget's mention
# autocomplete (see MENTION_JS in html_render.py), stripped before retrieval/prompting.
_MENTION_RE = re.compile(r"#(module|feature):([\w-]+)")


@dataclass(frozen=True)
class Turn:
    question: str
    answer: str


def _clip(text: str) -> str:
    return text if len(text) <= HISTORY_CHARS else text[:HISTORY_CHARS].rstrip() + "…"


class ConversationStore:
    """The recent turns of each chat session, in this process's memory and nowhere else.

    Deliberately not persisted. A conversation is worth remembering while the reader has the panel
    open; writing it to `.specky/` would turn every question anyone asks into a file on disk that
    nothing ever cleans up, in a directory that's meant to be disposable.

    Every dimension a caller controls is capped: turns per session, characters per turn, and
    sessions in total. The session id comes from the client, so none of them can be left open.
    """

    def __init__(self, max_turns: int = HISTORY_TURNS, max_sessions: int = MAX_SESSIONS) -> None:
        self._turns: OrderedDict[str, list[Turn]] = OrderedDict()
        self._max_turns = max_turns
        self._max_sessions = max_sessions
        # ThreadingHTTPServer answers each request on its own thread.
        self._lock = threading.Lock()

    @staticmethod
    def usable(session_id: str | None) -> bool:
        """Whether an id is one worth keeping history under. Anything else — absent, not a string,
        implausibly long — means this request is answered statelessly, as it was before sessions."""
        return isinstance(session_id, str) and 0 < len(session_id) <= SESSION_ID_MAX

    def history(self, session_id: str | None) -> tuple[Turn, ...]:
        if not self.usable(session_id):
            return ()
        with self._lock:
            turns = self._turns.get(session_id)
            if turns is None:
                return ()
            self._turns.move_to_end(session_id)
            return tuple(turns)

    def record(self, session_id: str | None, question: str, answer: str) -> None:
        if not self.usable(session_id):
            return
        with self._lock:
            turns = self._turns.setdefault(session_id, [])
            turns.append(Turn(_clip(question), _clip(answer)))
            del turns[: -self._max_turns]
            self._turns.move_to_end(session_id)
            while len(self._turns) > self._max_sessions:
                self._turns.popitem(last=False)

    def forget(self, session_id: str | None) -> None:
        """Drop a conversation. The widget's "New" button, so a reader isn't stuck talking to a
        transcript that survived into a question about something else."""
        if self.usable(session_id):
            with self._lock:
                self._turns.pop(session_id, None)


def _parse_scope(question: str) -> tuple[str, dict | None]:
    match = _MENTION_RE.search(question)
    scope = {"kind": match.group(1), "value": match.group(2)} if match else None
    clean = _MENTION_RE.sub("", question)
    return " ".join(clean.split()), scope


def classify_intent(question: str) -> str:
    """Which register a question is in: `INTENT_SPEC` if it's asking for a doc to be drafted,
    `INTENT_EXPLORE` otherwise. See `_SPEC_INTENT_PATTERNS` for why this is phrase-based."""
    if any(pattern.search(question) for pattern in _SPEC_INTENT_PATTERNS):
        return INTENT_SPEC
    return INTENT_EXPLORE


def coerce_intent(value: object) -> str | None:
    """A client-supplied intent, or None for "you decide". The widget's chips send one; anything
    that isn't one of `INTENTS` (a stale page, a hand-rolled caller) falls back to classifying."""
    return value if value in INTENTS else None


def _truncate(text: str, cap: int) -> str:
    """A body over its share of the budget, cut with the cut declared. Silent truncation is
    worse than none: the model would answer from half a doc believing it had the whole one."""
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + f"\n\n…[truncated — {cap} of {len(text)} characters shown]"


def _doc_bodies(conn, paths: Sequence[str]) -> dict[str, str]:
    """The indexed body of each doc, keyed by path. `documents.content` is the markdown with the
    frontmatter stripped, which is exactly what a reader would open the page to."""
    if not paths:
        return {}
    placeholders = ",".join("?" * len(paths))
    rows = conn.execute(
        f"SELECT path, content FROM documents WHERE path IN ({placeholders})", tuple(paths)
    ).fetchall()
    return {path: content for path, content in rows}


# The words a question is made of rather than the words it is about. Stripped before ranking,
# and only here: `fts_match_query()` is shared with `specky search`, where the reader typed the
# terms deliberately. A question doesn't work that way — "how is payment implemented" ORs to five
# terms, three of which are in every doc in the repo, so bm25 scored 30 docs within a rounding
# error of each other and the top five came back as MODULES.md, GLOSSARY.md and PRODUCT.md while
# the doc named payment-settlement-and-renewal.md ranked ninth.
_QUESTION_WORDS = frozenset(
    """a about an and any are as at be been but by can did do does for from get had has have how i
    if in into is it its me my no not of on or our should so than that the their them then there
    these they this to use used was we what when where which who why will with would you
    your""".split()
)
# Column order in documents_fts is (path, domain, title, content, tags). A term in the file name or
# the heading is the reader having named the topic; the same term buried in a long body is often
# just a cross-reference. Weighting the short columns up is what pulls a domain's own doc above the
# index pages that mention every term in the repo once.
_DOC_RANK = "bm25(documents_fts, 8.0, 2.0, 8.0, 1.0, 4.0)"


def _topic_match(question: str) -> str | None:
    """The FTS5 expression for a question: the words naming a topic, not the ones asking about it.
    Falls back to the whole question when stripping leaves nothing ("how does this work")."""
    topical = " ".join(
        word for word in re.findall(r"\w+", question) if word.lower() not in _QUESTION_WORDS
    )
    return fts_match_query(topical) or fts_match_query(question)


def retrieve_context(
    repo_root: Path, question: str, scope: dict | None = None, limit: int = DOC_HITS
) -> list[dict]:
    match = _topic_match(question)
    if match is None:
        return []

    conn = connect(repo_root)
    try:
        doc_rows = conn.execute(
            "SELECT path, domain, title, snippet(documents_fts, 3, '', '', '…', 40) "
            f"FROM documents_fts WHERE documents_fts MATCH ? ORDER BY {_DOC_RANK} LIMIT 50",
            (match,),
        ).fetchall()
        commit_rows = conn.execute(
            "SELECT sha, message, summary FROM commits_fts "
            "WHERE commits_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, min(limit, COMMIT_HITS)),
        ).fetchall()

        if scope is not None:
            if scope["kind"] == "module":
                scoped = [r for r in doc_rows if r[1] == scope["value"]]
            else:
                scoped = [r for r in doc_rows if Path(r[0]).stem == scope["value"]]
            doc_rows = scoped or doc_rows

        doc_rows = doc_rows[:limit]
        bodies = _doc_bodies(conn, [row[0] for row in doc_rows])
    finally:
        conn.close()

    context: list[dict] = []
    budget = CONTEXT_CHARS_MAX
    for path, _domain, title, excerpt in doc_rows:
        body = _truncate(bodies.get(path, ""), DOC_CHARS_MAX)
        # The best-ranked doc is sent whole even when it alone fills the budget — a repo whose
        # top hit is its one enormous doc must not get an excerpt-only answer about it.
        if body and (len(body) <= budget or not context):
            budget -= len(body)
            context.append({"source": path, "label": title, "text": body, "kind": "doc"})
        else:
            # Out of budget, or a path in the FTS table with no row behind it (an index written
            # by an older version). The search hit still names a doc worth reading.
            context.append({"source": path, "label": title, "text": excerpt, "kind": "excerpt"})
    context += [
        {
            "source": sha[:8],
            "label": message,
            "text": _truncate(summary, COMMIT_CHARS_MAX),
            "kind": "commit",
        }
        for sha, message, summary in commit_rows
        if summary
    ]
    return context


def _context_block(entry: dict) -> str:
    note = " (matching excerpt only — this doc was not included in full)" if (
        entry.get("kind") == "excerpt"
    ) else ""
    return f"[{entry['source']}] {entry['label']}{note}\n{entry['text']}"


def _build_prompt(
    question: str,
    context: list[dict],
    history: Sequence[Turn] = (),
    intent: str = INTENT_EXPLORE,
) -> str:
    blocks = (
        "\n\n".join(_context_block(c) for c in context)
        if context
        else "(no matching docs or commits found in the index)"
    )
    # Above the context, and labelled for what it is: the transcript is there to resolve what "it"
    # and "that one" refer to, while the answer still has to come out of the retrieved context. The
    # system prompt says ONLY the context, so without that sentence a follow-up gets refused.
    prior = ""
    if history:
        turns = "\n\n".join(f"Q: {t.question}\nA: {t.answer}" for t in history)
        prior = (
            "\nConversation so far — use it only to understand what the question refers to; the "
            f"answer must still come from the context below:\n{turns}\n"
        )
    instructions = _INTENT_INSTRUCTIONS[intent]
    return (
        f"{_GROUNDING_RULES}\n\n{instructions}\n{prior}\nContext:\n{blocks}\n\n"
        f"Question: {question}\nAnswer:"
    )


def answer_question(
    repo_root: Path, question: str, history: Sequence[Turn] = (), intent: str | None = None
) -> dict:
    """One question answered from the index, as both markdown and panel-ready HTML.

    `intent` pins the register (the panel's chips); None classifies the question. `answer` is the
    markdown the model wrote — it's what the conversation store replays and what the panel's
    "Copy markdown" hands the reader — and `answer_html` is that same answer rendered.
    """
    question, scope = _parse_scope(question)
    intent = intent or classify_intent(question)
    context = retrieve_context(repo_root, question, scope=scope)
    if not context and history:
        # A follow-up is often too short to retrieve on at all ("why?", "and the other one?"). The
        # previous question holds the words it left out, so ask the index again with those.
        context = retrieve_context(repo_root, f"{history[-1].question} {question}", scope=scope)
    provider = load_provider_from_toml(repo_root / "specky.toml", "serve")
    answer = provider.generate(
        _build_prompt(question, context, history, intent), task="chat"
    ).strip()
    # Imported here, not at module scope: html_render imports DEFAULT_PORT from this module, so a
    # top-level import of anything that reaches it would close a cycle (same reason `_search`
    # imports the indexer inside itself).
    from specky.answer_render import render_answer

    return {
        "answer": answer,
        "answer_html": render_answer(repo_root, answer),
        "sources": sorted({c["source"] for c in context}),
        "intent": intent,
    }


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
    # One store per server, not per module: two servers in one process (a test, a second repo)
    # shouldn't be able to see each other's conversations.
    conversations = ConversationStore()

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
            path = urlparse(self.path).path
            if path not in ("/chat", "/chat/reset"):
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                session = body.get("session")
                session = session if isinstance(session, str) else None
                if path == "/chat/reset":
                    conversations.forget(session)
                    self._json(200, {"reset": True})
                    return
                question = body.get("question", "").strip()
                if not question:
                    raise ValueError("question is required")
                result = answer_question(
                    repo_root,
                    question,
                    history=conversations.history(session),
                    intent=coerce_intent(body.get("intent")),
                )
                conversations.record(session, question, result["answer"])
                self._json(200, result)
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
