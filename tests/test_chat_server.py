import json
import threading
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from specky import chat_server, db
from specky.chat_server import (
    ServeConfig,
    _build_prompt,
    _extract_html_snippet,
    _parse_scope,
    answer_question,
    retrieve_context,
)
from specky.indexer import run_index

from conftest import FakeProvider


# --- mention scoping --------------------------------------------------------------------


def test_parse_scope_without_a_mention():
    assert _parse_scope("how do refunds work?") == ("how do refunds work?", None)


@pytest.mark.parametrize("kind", ["module", "feature"])
def test_parse_scope_strips_the_mention_from_the_question(kind):
    clean, scope = _parse_scope(f"#{kind}:refund-flow how does it work?")
    assert clean == "how does it work?"
    assert scope == {"kind": kind, "value": "refund-flow"}


def test_parse_scope_collapses_the_whitespace_the_mention_leaves_behind():
    clean, _ = _parse_scope("what about   #module:billing   limits?")
    assert clean == "what about limits?"


def test_parse_scope_ignores_a_bare_hash_word():
    assert _parse_scope("what is #billing") == ("what is #billing", None)


# --- html snippet extraction ------------------------------------------------------------


def test_extract_html_snippet_without_a_fence():
    assert _extract_html_snippet("  plain answer  ") == ("plain answer", None)


def test_extract_html_snippet_pulls_the_block_out_of_the_prose():
    prose, snippet = _extract_html_snippet("Refunds flow like so:\n```html\n<b>x</b>\n```\nDone.")
    assert prose == "Refunds flow like so:\n\nDone."
    assert snippet == "<b>x</b>"


def test_extract_html_snippet_is_case_insensitive():
    _, snippet = _extract_html_snippet("```HTML\n<i>y</i>\n```")
    assert snippet == "<i>y</i>"


# --- retrieval --------------------------------------------------------------------------


@pytest.fixture
def indexed_repo(tmp_repo, write_doc):
    write_doc(
        "billing/refund-flow.md",
        "# Billing — Refund Flow\n\nA refund returns money to the customer.\n",
        {"type": "workflow", "tags": ["refunds"]},
    )
    write_doc(
        "search/refund-report.md",
        "# Search — Refund Report\n\nReports on every refund issued.\n",
        {"type": "feature", "tags": ["refunds"]},
    )
    run_index(tmp_repo)
    return tmp_repo


def test_retrieve_context_returns_nothing_for_an_unsearchable_question(indexed_repo):
    assert retrieve_context(indexed_repo, "???") == []


def test_retrieve_context_finds_matching_docs(indexed_repo):
    sources = {c["source"] for c in retrieve_context(indexed_repo, "refund")}
    assert sources == {"specs/billing/refund-flow.md", "specs/search/refund-report.md"}


def test_module_scope_narrows_to_one_domain(indexed_repo):
    context = retrieve_context(indexed_repo, "refund", scope={"kind": "module", "value": "billing"})
    assert [c["source"] for c in context] == ["specs/billing/refund-flow.md"]


def test_feature_scope_narrows_to_one_doc_stem(indexed_repo):
    context = retrieve_context(
        indexed_repo, "refund", scope={"kind": "feature", "value": "refund-report"}
    )
    assert [c["source"] for c in context] == ["specs/search/refund-report.md"]


def test_a_scope_matching_nothing_falls_back_to_the_unscoped_hits(indexed_repo):
    """Better a broad answer than an empty one — the mention is a hint, not a filter."""
    context = retrieve_context(indexed_repo, "refund", scope={"kind": "module", "value": "nope"})
    assert len(context) == 2


def test_retrieve_context_honours_the_limit(indexed_repo):
    assert len(retrieve_context(indexed_repo, "refund", limit=1)) == 1


def test_retrieve_context_includes_commits_with_a_micro_doc_summary(indexed_repo):
    """A commit only carries retrievable text once `specky commit-doc` has written its
    micro_doc summary; index_commits copies that into commits_fts."""
    conn = db.connect(indexed_repo)
    sha = conn.execute("SELECT sha FROM commits").fetchone()[0]
    conn.execute(
        "INSERT INTO micro_docs (sha, summary, created_at) VALUES (?, ?, ?)",
        (sha, "Set up the refund tables.", "2026-01-01"),
    )
    conn.commit()
    conn.close()
    run_index(indexed_repo)

    context = retrieve_context(indexed_repo, "refund")
    assert sha[:8] in {c["source"] for c in context}


# --- prompt + end to end ----------------------------------------------------------------


def test_build_prompt_says_so_when_nothing_matched():
    assert "(no matching docs or commits found in the index)" in _build_prompt("q", [])


def test_build_prompt_labels_each_context_block():
    prompt = _build_prompt("q", [{"source": "specs/a.md", "label": "A", "text": "body"}])
    assert "[specs/a.md] A\nbody" in prompt
    assert prompt.rstrip().endswith("Question: q\nAnswer:")


def test_answer_question_scopes_retrieval_and_reports_sources(indexed_repo, monkeypatch):
    provider = FakeProvider("Refunds work like this.")
    monkeypatch.setattr(
        chat_server, "load_provider_from_toml", lambda _path, _command="": provider
    )

    result = answer_question(indexed_repo, "#module:billing how do refunds work?")

    assert result == {"answer": "Refunds work like this.", "sources": ["specs/billing/refund-flow.md"]}
    assert "#module:billing" not in provider.prompts[0]  # the mention never reaches the model


def test_answer_question_returns_an_html_snippet_when_the_model_sends_one(indexed_repo, monkeypatch):
    provider = FakeProvider("Here:\n```html\n<table><tr><td>1</td></tr></table>\n```")
    monkeypatch.setattr(
        chat_server, "load_provider_from_toml", lambda _path, _command="": provider
    )

    result = answer_question(indexed_repo, "refund table?")
    assert result["answer"] == "Here:"
    assert result["html_snippet"].startswith("<table>")


# --- [serve] configuration ---------------------------------------------------------------


def test_serve_config_defaults_to_todays_behaviour(tmp_repo):
    """No specky.toml, or one without a [serve] table: loopback, open, no token. An existing
    repo must not need a config change to keep working."""
    config = ServeConfig.load(tmp_repo)
    assert (config.host, config.port, config.allow_origins, config.token) == (
        "127.0.0.1",
        chat_server.DEFAULT_PORT,
        ("*",),
        "",
    )
    assert config.open_to_everyone


def test_serve_config_reads_the_table_and_cli_flags_win(tmp_repo):
    (tmp_repo / "specky.toml").write_text(
        '[serve]\nhost = "0.0.0.0"\nport = 9000\n'
        'allow_origins = ["https://docs.example"]\ntoken = "s3cret"\n'
    )
    config = ServeConfig.load(tmp_repo)
    assert (config.host, config.port, config.token) == ("0.0.0.0", 9000, "s3cret")
    assert not config.open_to_everyone

    override = ServeConfig.load(tmp_repo, host="127.0.0.1", port=9999)
    assert (override.host, override.port) == ("127.0.0.1", 9999)
    assert override.token == "s3cret"  # untouched by the flags


def test_a_single_origin_string_is_accepted(tmp_repo):
    (tmp_repo / "specky.toml").write_text('[serve]\nallow_origins = "null"\n')
    assert ServeConfig.load(tmp_repo).allow_origins == ("null",)


@pytest.mark.parametrize(
    "origins, origin, allowed",
    [
        (("*",), "https://evil.example", True),  # the default lets anything through
        (("https://docs.example",), "https://docs.example", True),
        (("https://docs.example",), "https://evil.example", False),
        (("https://docs.example",), None, True),  # non-browser caller, never protected here
        (("null",), "null", True),  # what a file:// page sends
    ],
)
def test_origin_allowlist(origins, origin, allowed):
    assert ServeConfig(allow_origins=origins).origin_allowed(origin) is allowed


def test_a_narrowed_allowlist_echoes_the_origin_rather_than_a_wildcard():
    """`*` back to a narrowed allowlist would hand every other origin access too."""
    config = ServeConfig(allow_origins=("https://docs.example",))
    assert config.acao_for("https://docs.example") == "https://docs.example"
    assert ServeConfig().acao_for("https://docs.example") == "*"


@pytest.mark.parametrize(
    "token, supplied, ok",
    [("", None, True), ("", "anything", True), ("s3cret", None, False), ("s3cret", "s3cret", True)],
)
def test_token_check(token, supplied, ok):
    assert ServeConfig(token=token).token_ok(supplied) is ok


@pytest.mark.parametrize(
    "host, loopback",
    [("127.0.0.1", True), ("localhost", True), ("::1", True), ("0.0.0.0", False), ("::", False)],
)
def test_loopback_detection_drives_the_exposure_warning(host, loopback):
    assert chat_server._is_loopback(host) is loopback


# --- the HTTP surface --------------------------------------------------------------------


@contextmanager
def _running(repo_root: Path, config: ServeConfig = ServeConfig()):
    """The real handler on an ephemeral port — the header and path handling under test only
    exist inside BaseHTTPRequestHandler, so there's nothing to unit-test underneath it."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), chat_server._make_handler(repo_root, config))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(port, method, path, *, origin=None, token=None, body=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    if token is not None:
        headers[chat_server.TOKEN_HEADER] = token
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=payload, headers=headers)
        res = conn.getresponse()
        return res.status, res.headers, res.read()
    finally:
        conn.close()


@pytest.fixture
def answering(monkeypatch):
    """Stub the whole retrieval+provider path: these tests are about access control."""
    asked = []
    monkeypatch.setattr(
        chat_server,
        "answer_question",
        lambda _root, question: asked.append(question) or {"answer": "ok", "sources": []},
    )
    return asked


def test_chat_answers_any_origin_under_the_default_config(tmp_repo, answering):
    with _running(tmp_repo) as port:
        status, headers, raw = _request(
            port, "POST", "/chat", origin="https://anywhere.example", body={"question": "refunds?"}
        )
    assert status == 200
    assert json.loads(raw)["answer"] == "ok"
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert answering == ["refunds?"]


def test_a_disallowed_origin_gets_403_and_no_cors_headers(tmp_repo, answering):
    """No `Access-Control-Allow-Origin` on the rejection either, so the calling page can't even
    read the error — and no provider call is made."""
    config = ServeConfig(allow_origins=("https://docs.example",))
    with _running(tmp_repo, config) as port:
        status, headers, raw = _request(
            port, "POST", "/chat", origin="https://evil.example", body={"question": "refunds?"}
        )
    assert status == 403
    assert "allow_origins" in json.loads(raw)["error"]
    assert "Access-Control-Allow-Origin" not in headers
    assert answering == []


def test_an_allowed_origin_is_echoed_and_varied_on(tmp_repo, answering):
    config = ServeConfig(allow_origins=("https://docs.example",))
    with _running(tmp_repo, config) as port:
        status, headers, _ = _request(
            port, "POST", "/chat", origin="https://docs.example", body={"question": "q"}
        )
    assert status == 200
    assert headers["Access-Control-Allow-Origin"] == "https://docs.example"
    # Without Vary, a shared cache can serve one origin's response (and its ACAO) to another.
    assert headers["Vary"] == "Origin"


def test_the_preflight_is_rejected_for_a_disallowed_origin(tmp_repo, answering):
    config = ServeConfig(allow_origins=("https://docs.example",))
    with _running(tmp_repo, config) as port:
        assert _request(port, "OPTIONS", "/chat", origin="https://evil.example")[0] == 403
        status, headers, _ = _request(port, "OPTIONS", "/chat", origin="https://docs.example")
    assert status == 204
    assert chat_server.TOKEN_HEADER in headers["Access-Control-Allow-Headers"]


def test_a_configured_token_is_required(tmp_repo, answering):
    with _running(tmp_repo, ServeConfig(token="s3cret")) as port:
        no_token = _request(port, "POST", "/chat", body={"question": "q"})
        wrong = _request(port, "POST", "/chat", token="guess", body={"question": "q"})
        right = _request(port, "POST", "/chat", token="s3cret", body={"question": "q"})
    assert (no_token[0], wrong[0], right[0]) == (403, 403, 200)
    assert chat_server.TOKEN_HEADER in json.loads(no_token[2])["error"]
    assert answering == ["q"]  # only the authenticated call reached the provider


def test_an_unknown_post_path_is_404_not_a_chat_call(tmp_repo, answering):
    with _running(tmp_repo) as port:
        assert _request(port, "POST", "/whatever", body={"question": "q"})[0] == 404
    assert answering == []


# --- serving the rendered site ------------------------------------------------------------


@pytest.fixture
def rendered_site(tmp_repo) -> Path:
    site = tmp_repo / ".specky" / "site"
    (site / "assets").mkdir(parents=True)
    (site / "index.html").write_text("<h1>docs</h1>")
    (site / "assets" / "site.css").write_text(":root{}")
    (tmp_repo / "secret.txt").write_text("not part of the site")
    return site


def test_the_site_is_served_from_the_same_origin_as_chat(tmp_repo, rendered_site):
    """This is what makes a deployed viewer need no CORS at all: one port serves both."""
    with _running(tmp_repo) as port:
        root = _request(port, "GET", "/")
        css = _request(port, "GET", "/assets/site.css")
    assert (root[0], root[2]) == (200, b"<h1>docs</h1>")
    assert root[1]["Content-Type"] == "text/html"
    assert (css[0], css[2]) == (200, b":root{}")
    assert css[1]["Content-Type"] == "text/css"


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/assets/../../secret.txt",
        "/%2e%2e/secret.txt",  # unquoted after the split, so the check has to run on the result
        "/etc/passwd",
    ],
)
def test_nothing_outside_the_site_directory_is_reachable(tmp_repo, rendered_site, path):
    with _running(tmp_repo) as port:
        status, _, raw = _request(port, "GET", path)
    assert status == 404
    assert b"not part of the site" not in raw


def test_an_unrendered_site_says_which_command_to_run(tmp_repo):
    with _running(tmp_repo) as port:
        status, _, raw = _request(port, "GET", "/")
    assert status == 404
    assert "specky render-html" in json.loads(raw)["error"]


def test_a_disallowed_origin_cannot_read_the_site_either(tmp_repo, rendered_site):
    config = ServeConfig(allow_origins=("https://docs.example",))
    with _running(tmp_repo, config) as port:
        assert _request(port, "GET", "/", origin="https://evil.example")[0] == 403


def test_the_site_needs_no_token_even_when_the_api_does(tmp_repo, rendered_site):
    """A page can't attach a header to its own `<link>`/`<script>` loads, so token-gating static
    files would make the served viewer unopenable."""
    with _running(tmp_repo, ServeConfig(token="s3cret")) as port:
        assert _request(port, "GET", "/assets/site.css")[0] == 200


# --- GET /search: the viewer's search box against the real index ---------------------------


def test_search_returns_ranked_fts_hits(indexed_repo):
    """The viewer ships a bounded slice of each doc; this endpoint has the whole index, which is
    the only search that stays exact as a repo grows."""
    with _running(indexed_repo) as port:
        status, _, raw = _request(port, "GET", "/search?q=refund")
    results = json.loads(raw)["results"]

    assert status == 200
    assert {r["path"] for r in results} == {
        "specs/billing/refund-flow.md",
        "specs/search/refund-report.md",
    }
    assert all({"path", "domain", "title", "snippet"} == set(r) for r in results)


def test_search_finds_a_phrase_the_shipped_excerpt_would_miss(tmp_repo, write_doc):
    """The whole point: an excerpt stops after 160 chars, the index doesn't."""
    write_doc(
        "billing/refund-flow.md",
        "# Refunds\n\n" + ("Filler prose. " * 60) + "\nThe cutoff is a hyperbolic tangent.\n",
    )
    run_index(tmp_repo)
    with _running(tmp_repo) as port:
        status, _, raw = _request(port, "GET", "/search?q=hyperbolic")
    assert status == 200
    assert [r["path"] for r in json.loads(raw)["results"]] == ["specs/billing/refund-flow.md"]


def test_search_without_a_query_is_a_400(indexed_repo):
    with _running(indexed_repo) as port:
        blank = _request(port, "GET", "/search?q=%20%20")
        missing = _request(port, "GET", "/search")
    assert blank[0] == missing[0] == 400
    assert "q is required" in json.loads(blank[2])["error"]


def test_search_syntax_that_would_break_fts5_is_answered_not_crashed(indexed_repo):
    """`fts_match_query` is what keeps a user's punctuation out of FTS5's grammar."""
    with _running(indexed_repo) as port:
        status, _, raw = _request(port, "GET", "/search?q=%22refund")  # a lone double quote
    assert status == 200
    assert isinstance(json.loads(raw)["results"], list)


def test_search_caps_the_row_count_a_caller_can_ask_for(indexed_repo, monkeypatch):
    asked = {}

    def record(root, query, limit=10):
        asked["limit"] = limit
        return []

    monkeypatch.setattr("specky.indexer.search", record)
    with _running(indexed_repo) as port:
        _request(port, "GET", "/search?q=refund&limit=100000")
    assert asked["limit"] == chat_server.SEARCH_LIMIT_MAX


def test_a_junk_limit_falls_back_to_the_default(indexed_repo):
    with _running(indexed_repo) as port:
        assert _request(port, "GET", "/search?q=refund&limit=lots")[0] == 200


def test_search_obeys_the_origin_and_token_policy(indexed_repo):
    """It reads doc text, so it's API surface, not static files — same gate as /chat."""
    config = ServeConfig(allow_origins=("https://docs.example",), token="s3cret")
    with _running(indexed_repo, config) as port:
        wrong_origin = _request(port, "GET", "/search?q=refund", origin="https://evil.example")
        no_token = _request(port, "GET", "/search?q=refund", origin="https://docs.example")
        allowed = _request(
            port, "GET", "/search?q=refund", origin="https://docs.example", token="s3cret"
        )
    assert (wrong_origin[0], no_token[0], allowed[0]) == (403, 403, 200)
