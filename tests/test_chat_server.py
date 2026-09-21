import json
import threading
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from specky import chat_server, db
from specky.chat_server import (
    INTENT_EXPLORE,
    INTENT_SPEC,
    ConversationStore,
    ServeConfig,
    Turn,
    _build_prompt,
    MORE_MARKER,
    _parse_scope,
    answer_question,
    classify_intent,
    coerce_intent,
    retrieve_context,
    split_answer,
)
from specky.doc_tools import topic_match as _topic_match
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


# --- intent ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "write a spec for bulk refunds",
        "draft the documentation for the new export flow",
        "can you create a feature doc for saved searches?",
        "propose requirements for rate limiting",
        "what are the acceptance criteria for a partial refund?",
        "acceptance tests for the retry path",
        "requirements for the billing webhook",
        "how should we handle a failed payout?",
        "we need to support multi-currency invoices",
        "spec out the audit log",
        "outline a user story for onboarding",
    ],
)
def test_a_question_asking_for_a_draft_is_spec_intent(question):
    assert classify_intent(question) == INTENT_SPEC


@pytest.mark.parametrize(
    "question",
    [
        "how do refunds work?",
        "where is the retry added?",
        "which doc describes the export flow?",
        "what does the acceptance test table cover?",
        "who owns the billing docs",
        "why is a spec split by domain?",
        "when was the webhook feature documented?",
        "does the exporter create a PDF?",
    ],
)
def test_a_question_asking_about_the_docs_is_explore_intent(question):
    assert classify_intent(question) == INTENT_EXPLORE


@pytest.mark.parametrize(
    "value,expected",
    [
        (INTENT_SPEC, INTENT_SPEC),
        (INTENT_EXPLORE, INTENT_EXPLORE),
        ("auto", None),
        ("", None),
        (None, None),
        (7, None),
        (["spec"], None),
    ],
)
def test_only_a_known_intent_is_taken_from_the_client(value, expected):
    assert coerce_intent(value) == expected


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


def test_the_words_a_question_is_made_of_do_not_decide_the_ranking(tmp_repo, write_doc):
    """"how is X implemented" used to rank the doc mentioning "how" and "is" the most often, which
    on a real tree meant MODULES.md and GLOSSARY.md — every question's top hits."""
    write_doc(
        "billing/refund-flow.md",
        "# Billing — Refund Flow\n\nHow a refund is issued and how it is settled.\n",
        {"type": "workflow", "tags": ["refunds"]},
    )
    write_doc(
        "MODULES.md",
        "# Modules\n\nHow this is laid out: how it is built, what is where, how to use it.\n",
        {},
    )
    run_index(tmp_repo)
    context = retrieve_context(tmp_repo, "how is a refund implemented")
    assert [c["source"] for c in context] == ["specs/billing/refund-flow.md"]


def test_a_question_of_nothing_but_question_words_still_searches():
    """Stripping must never empty the query — a vague question gets vague hits, not none."""
    assert _topic_match("how is a refund implemented") == db.fts_match_query("refund implemented")
    assert _topic_match("what does this do") == db.fts_match_query("what does this do")


def test_the_docs_own_topic_outranks_a_passing_mention_of_it(tmp_repo, write_doc):
    """A term in the file name or the title is the reader naming the topic. The same term inside a
    long body is often a cross-reference, so the short columns are weighted up."""
    write_doc(
        "chat/chat-attachments.md",
        "# Chat — Attachments\n\nWhat a customer may upload into a conversation.\n",
        {"type": "feature", "tags": ["chat"]},
    )
    write_doc(
        "billing/invoices.md",
        "# Billing — Invoices\n\n" + ("An invoice may carry attachments. " * 60),
        {"type": "feature", "tags": ["billing"]},
    )
    run_index(tmp_repo)
    context = retrieve_context(tmp_repo, "how do chat attachments work")
    assert context[0]["source"] == "specs/chat/chat-attachments.md"


def test_retrieve_context_sends_the_whole_doc_not_a_snippet(indexed_repo):
    """The index locates the doc; the doc itself is the grounding. An excerpt around the match
    is enough to rank a doc and not enough to answer from it."""
    entry = next(
        c for c in retrieve_context(indexed_repo, "refund")
        if c["source"] == "specs/billing/refund-flow.md"
    )
    assert entry["kind"] == "doc"
    assert entry["text"] == "# Billing — Refund Flow\n\nA refund returns money to the customer.\n"


def test_an_oversized_doc_is_truncated_and_says_so(indexed_repo, write_doc, monkeypatch):
    write_doc("billing/refund-long.md", "# Long\n\n" + "refund " * 4000, {"tags": ["refunds"]})
    run_index(indexed_repo)
    monkeypatch.setattr(chat_server, "DOC_CHARS_MAX", 500)

    entry = next(
        c for c in retrieve_context(indexed_repo, "refund")
        if c["source"] == "specs/billing/refund-long.md"
    )
    assert len(entry["text"]) < 600
    assert "truncated" in entry["text"]


def test_docs_past_the_budget_fall_back_to_their_excerpt(indexed_repo, monkeypatch):
    """Degrading to the old behaviour is the right failure: the doc is still named and citable,
    so the answer can point at it instead of pretending it wasn't found."""
    monkeypatch.setattr(chat_server, "CONTEXT_CHARS_MAX", 80)

    kinds = [c["kind"] for c in retrieve_context(indexed_repo, "refund")]
    assert kinds.count("doc") == 1  # the best-ranked hit
    assert kinds.count("excerpt") == 1


def test_the_best_ranked_doc_is_sent_whole_even_when_it_alone_exceeds_the_budget(
    indexed_repo, monkeypatch
):
    monkeypatch.setattr(chat_server, "CONTEXT_CHARS_MAX", 1)

    first = retrieve_context(indexed_repo, "refund")[0]
    assert first["kind"] == "doc"
    assert "returns money" in first["text"] or "every refund" in first["text"]


def test_a_commit_summary_is_sent_whole(indexed_repo):
    conn = db.connect(indexed_repo)
    sha = conn.execute("SELECT sha FROM commits").fetchone()[0]
    summary = "Set up the refund tables. " + "It also reworks the ledger. " * 20
    conn.execute(
        "INSERT INTO micro_docs (sha, summary, created_at) VALUES (?, ?, ?)",
        (sha, summary, "2026-01-01"),
    )
    conn.commit()
    conn.close()
    run_index(indexed_repo)

    entry = next(c for c in retrieve_context(indexed_repo, "refund") if c["source"] == sha[:8])
    assert entry["text"] == summary


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


def test_build_prompt_marks_a_block_that_is_only_an_excerpt():
    """The model has to be able to tell "here is the doc" from "here is a fragment of one", or it
    answers from the fragment."""
    prompt = _build_prompt(
        "q", [{"source": "specs/a.md", "label": "A", "text": "…bit…", "kind": "excerpt"}]
    )
    assert "[specs/a.md] A (matching excerpt only — this doc was not included in full)" in prompt


def test_build_prompt_does_not_mark_a_whole_doc():
    prompt = _build_prompt(
        "q", [{"source": "specs/a.md", "label": "A", "text": "body", "kind": "doc"}]
    )
    assert "excerpt only" not in prompt


def test_build_prompt_asks_for_the_short_answer_first_and_stays_grounded():
    prompt = _build_prompt("q", [])

    assert "Answer the reader's question" in prompt
    assert "short answer" in prompt and MORE_MARKER in prompt
    assert "ONLY the context below" in prompt
    # Drafting is spec_draft's job now; the explore prompt never asks for a doc.
    assert "specs/<domain>/<topic>.md" not in prompt


# --- short answer + read more ---------------------------------------------------------------


def test_split_answer_splits_on_the_marker():
    short, details = split_answer(f"Refunds take 5 days.\n\n{MORE_MARKER}\n\n## Why\n\nBatching.")
    assert (short, details) == ("Refunds take 5 days.", "## Why\n\nBatching.")


def test_a_short_only_answer_has_no_details():
    assert split_answer("Yes — see specs/billing/refund-flow.md.") == (
        "Yes — see specs/billing/refund-flow.md.",
        "",
    )


def test_without_the_marker_the_first_paragraph_is_the_short_answer():
    short, details = split_answer("Refunds take 5 days.\n\n| Case | Days |\n|---|---|\n| A | 5 |")
    assert short == "Refunds take 5 days."
    assert details.startswith("| Case | Days |")


def test_an_answer_that_opens_with_structure_is_shown_whole():
    """Hiding a table behind "Read more" with nothing in front of it would show the reader an empty
    answer and a button."""
    text = "## Refunds\n\n| Case | Result |\n|---|---|\n| Full | Refunded |"
    assert split_answer(text) == (text, "")


def test_a_marker_with_nothing_before_it_still_leaves_a_short_answer():
    assert split_answer(f"{MORE_MARKER}\nAll of it.") == ("All of it.", "")


def test_answer_question_returns_the_details_separately(indexed_repo, monkeypatch):
    provider = FakeProvider(f"A refund returns money.\n\n{MORE_MARKER}\n\n## Steps\n\n1. Ask.")
    monkeypatch.setattr(
        chat_server, "load_provider_from_toml", lambda _path, _command="": provider
    )

    result = answer_question(indexed_repo, "how do refunds work?")

    assert "A refund returns money." in result["answer_html"]
    assert "<h2>Steps</h2>" in result["details_html"]
    assert "<h2>" not in result["answer_html"]
    assert MORE_MARKER not in result["answer"]  # Copy markdown gets clean markdown
    assert result["answer"].startswith("A refund returns money.")


def test_answer_question_scopes_retrieval_and_reports_sources(indexed_repo, monkeypatch):
    provider = FakeProvider("Refunds work like this.")
    monkeypatch.setattr(
        chat_server, "load_provider_from_toml", lambda _path, _command="": provider
    )

    result = answer_question(indexed_repo, "#module:billing how do refunds work?")

    assert result["answer"] == "Refunds work like this."
    assert result["sources"] == ["specs/billing/refund-flow.md"]
    assert result["intent"] == INTENT_EXPLORE
    assert "#module:billing" not in provider.prompts[0]  # the mention never reaches the model


def test_answer_question_renders_the_answer_for_the_panel(indexed_repo, monkeypatch):
    provider = FakeProvider("## Refunds\n\n| Case | Result |\n|---|---|\n| Full | Refunded |")
    monkeypatch.setattr(
        chat_server, "load_provider_from_toml", lambda _path, _command="": provider
    )

    result = answer_question(indexed_repo, "how do refunds work?")

    assert result["answer"].startswith("## Refunds")  # the markdown survives, for Copy markdown
    assert "<h2>Refunds</h2>" in result["answer_html"]
    assert '<figure class="tw">' in result["answer_html"]


def test_a_spec_request_starts_the_draft_workflow(indexed_repo, monkeypatch):
    """Drafting is a staged workflow now (spec_draft.py), not one prompt — and the mention the
    reader typed reaches it as a scope, not as text."""
    from specky import spec_draft

    provider = FakeProvider("unused")
    started = []
    monkeypatch.setattr(
        chat_server, "load_provider_from_toml", lambda _path, _command="": provider
    )
    monkeypatch.setattr(
        spec_draft,
        "start",
        lambda root, prov, request, mention=None: started.append((request, mention))
        or {"intent": INTENT_SPEC, "draft": {}},
    )

    result = answer_question(indexed_repo, "#module:billing write a spec for partial refunds")

    assert result["intent"] == INTENT_SPEC
    assert started == [("write a spec for partial refunds", {"kind": "module", "value": "billing"})]
    assert provider.prompts == []  # the explore path never ran


def test_an_explicit_intent_beats_the_classifier(indexed_repo, monkeypatch):
    """The panel's chips exist for the question the classifier reads the other way round."""
    provider = FakeProvider("prose")
    monkeypatch.setattr(
        chat_server, "load_provider_from_toml", lambda _path, _command="": provider
    )

    result = answer_question(
        indexed_repo, "write a spec for partial refunds", intent=INTENT_EXPLORE
    )

    assert result["intent"] == INTENT_EXPLORE
    assert "specs/<domain>/<topic>.md" not in provider.prompts[0]


# --- conversation memory ------------------------------------------------------------------


def test_an_unknown_session_has_no_history():
    assert ConversationStore().history("nobody") == ()


def test_a_recorded_turn_comes_back_for_that_session():
    store = ConversationStore()
    store.record("s1", "how do refunds work?", "Like this.")
    assert store.history("s1") == (Turn("how do refunds work?", "Like this."),)
    assert store.history("s2") == ()  # conversations don't leak into each other


def test_only_the_last_few_turns_are_kept():
    """The cap is a cost bound, not a nicety: every kept turn is re-sent to the provider on every
    later question in that conversation."""
    store = ConversationStore(max_turns=2)
    for n in range(4):
        store.record("s1", f"q{n}", f"a{n}")
    assert [t.question for t in store.history("s1")] == ["q2", "q3"]


def test_a_long_turn_is_clipped_rather_than_stored_whole():
    store = ConversationStore()
    store.record("s1", "q" * 5000, "a" * 5000)
    turn = store.history("s1")[0]
    assert len(turn.question) == chat_server.HISTORY_CHARS + 1  # the ellipsis
    assert turn.answer.endswith("…")


def test_the_least_recently_used_conversation_is_dropped_first():
    """A session id is whatever the caller sent, so the number of them has to be bounded — but
    bounded in a way that drops the conversation nobody is still having."""
    store = ConversationStore(max_sessions=2)
    store.record("s1", "q1", "a1")
    store.record("s2", "q2", "a2")
    store.history("s1")  # s1 is the one still in use
    store.record("s3", "q3", "a3")

    assert store.history("s1") != ()
    assert store.history("s2") == ()
    assert store.history("s3") != ()


@pytest.mark.parametrize("session", [None, "", "x" * (chat_server.SESSION_ID_MAX + 1), 17, {}])
def test_an_unusable_session_id_is_answered_statelessly(session):
    """No id, a junk id, or one long enough to be someone filling memory with keys: the request
    still works, it just remembers nothing."""
    store = ConversationStore()
    store.record(session, "q", "a")
    assert store.history(session) == ()


def test_forget_drops_a_conversation():
    store = ConversationStore()
    store.record("s1", "q", "a")
    store.forget("s1")
    assert store.history("s1") == ()


def test_a_prompt_without_history_is_unchanged():
    """The stateless prompt is what every existing test and every cached response was built on."""
    assert "Conversation so far" not in _build_prompt("q", [])


def test_prior_turns_sit_above_the_context_and_are_labelled_as_reference_only():
    prompt = _build_prompt(
        "why?",
        [{"source": "specs/a.md", "label": "A", "text": "body"}],
        history=(Turn("how do refunds work?", "Like this."),),
    )
    assert "Q: how do refunds work?\nA: Like this." in prompt
    assert prompt.index("Conversation so far") < prompt.index("Context:")
    # The system prompt says to answer from the context ONLY, so a transcript above it has to say
    # what it's for or the model refuses the follow-up.
    assert "answer must still come from the context below" in prompt


def test_a_follow_up_too_short_to_retrieve_on_reuses_the_previous_question(
    indexed_repo, monkeypatch
):
    provider = FakeProvider("Because it does.")
    monkeypatch.setattr(
        chat_server, "load_provider_from_toml", lambda _path, _command="": provider
    )

    cold = answer_question(indexed_repo, "why?")
    warm = answer_question(indexed_repo, "why?", history=(Turn("refund flow", "Like this."),))

    assert cold["sources"] == []  # "why?" alone matches nothing in the index
    assert "specs/billing/refund-flow.md" in warm["sources"]


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
    """Stub the whole retrieval+provider path: these tests are about access control and session
    plumbing. Each entry is one `(question, history, intent)` the handler passed down."""
    asked = []

    def fake(_root, question, history=(), intent=None):
        asked.append((question, tuple(history), intent))
        return {
            "answer": f"answer to {question}",
            "answer_html": f"<p>answer to {question}</p>",
            "sources": [],
            "intent": intent or INTENT_EXPLORE,
        }

    monkeypatch.setattr(chat_server, "answer_question", fake)
    return asked


def _questions(asked) -> list[str]:
    return [question for question, _history, _intent in asked]


def test_chat_answers_any_origin_under_the_default_config(tmp_repo, answering):
    with _running(tmp_repo) as port:
        status, headers, raw = _request(
            port, "POST", "/chat", origin="https://anywhere.example", body={"question": "refunds?"}
        )
    assert status == 200
    assert json.loads(raw)["answer"] == "answer to refunds?"
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert _questions(answering) == ["refunds?"]


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
    assert _questions(answering) == ["q"]  # only the authenticated call reached the provider


def test_an_unknown_post_path_is_404_not_a_chat_call(tmp_repo, answering):
    with _running(tmp_repo) as port:
        assert _request(port, "POST", "/whatever", body={"question": "q"})[0] == 404
    assert answering == []


def test_chat_passes_the_intent_the_panels_chips_sent(tmp_repo, answering):
    with _running(tmp_repo) as port:
        status, _, raw = _request(
            port, "POST", "/chat", body={"question": "q", "intent": INTENT_SPEC}
        )
    assert status == 200
    assert json.loads(raw)["intent"] == INTENT_SPEC
    assert answering[0][2] == INTENT_SPEC


@pytest.mark.parametrize("intent", ["draft", "", 7, None, {"mode": "spec"}])
def test_chat_ignores_an_intent_it_does_not_know(tmp_repo, answering, intent):
    """A stale page or a hand-rolled caller can send anything; an unknown value means "you
    decide", not an error and not a prompt built from client-supplied text."""
    with _running(tmp_repo) as port:
        status, _, _ = _request(port, "POST", "/chat", body={"question": "q", "intent": intent})
    assert status == 200
    assert answering[0][2] is None  # classify_intent decides


def test_a_draft_response_without_an_answer_is_not_recorded(tmp_repo, monkeypatch):
    """A draft's first response carries its state, not an answer — nothing to replay."""
    monkeypatch.setattr(
        chat_server,
        "answer_question",
        lambda *_a, **_k: {"intent": INTENT_SPEC, "draft": {"stage": "impact"}, "sources": []},
    )
    with _running(tmp_repo) as port:
        status, _, raw = _request(
            port, "POST", "/chat", body={"question": "write a spec", "session": "s1"}
        )
    assert status == 200
    assert json.loads(raw)["draft"] == {"stage": "impact"}


@pytest.fixture
def drafting(monkeypatch):
    """Stub the draft step: these tests are about the route, not the workflow."""
    seen = []

    def fake(_root, body):
        seen.append(body)
        if body.get("action") == "explode":
            from specky.spec_draft import DraftError

            raise DraftError("Unknown draft action 'explode'.")
        return {"intent": INTENT_SPEC, "draft": {"stage": "acceptance"}, "sources": []}

    monkeypatch.setattr(chat_server, "draft_step", fake)
    return seen


def test_draft_steps_are_routed_to_the_workflow(tmp_repo, drafting):
    body = {"action": "confirm", "state": {"request": "r"}}
    with _running(tmp_repo) as port:
        status, _, raw = _request(port, "POST", "/draft", body=body)
    assert status == 200
    assert json.loads(raw)["draft"]["stage"] == "acceptance"
    assert drafting == [body]


def test_a_draft_error_is_a_400_the_panel_can_show(tmp_repo, drafting):
    with _running(tmp_repo) as port:
        status, _, raw = _request(port, "POST", "/draft", body={"action": "explode"})
    assert status == 400
    assert "Unknown draft action" in json.loads(raw)["error"]


def test_draft_obeys_the_origin_and_token_policy(tmp_repo, drafting):
    config = ServeConfig(allow_origins=("http://docs.example",), token="s3cret")
    with _running(tmp_repo, config) as port:
        denied = _request(port, "POST", "/draft", body={}, origin="http://evil.example")
        no_token = _request(port, "POST", "/draft", body={}, origin="http://docs.example")
        allowed = _request(
            port, "POST", "/draft", body={}, origin="http://docs.example", token="s3cret"
        )
    assert (denied[0], no_token[0], allowed[0]) == (403, 403, 200)
    assert len(drafting) == 1


def test_draft_step_hands_the_body_to_the_workflow(tmp_repo, monkeypatch):
    from specky import spec_draft

    calls = []
    provider = FakeProvider("unused")
    monkeypatch.setattr(
        chat_server, "load_provider_from_toml", lambda _path, _command="": provider
    )
    monkeypatch.setattr(
        spec_draft,
        "act",
        lambda root, prov, action, state, reply=None, option=None: calls.append(
            (prov, action, state, reply, option)
        )
        or {},
    )

    chat_server.draft_step(tmp_repo, {"action": "choose", "state": {"x": 1}, "option": 2})

    assert calls == [(provider, "choose", {"x": 1}, None, 2)]


# --- sessions over HTTP -------------------------------------------------------------------


def test_a_follow_up_on_the_same_session_carries_the_previous_turn(tmp_repo, answering):
    with _running(tmp_repo) as port:
        _request(port, "POST", "/chat", body={"question": "first", "session": "s1"})
        _request(port, "POST", "/chat", body={"question": "second", "session": "s1"})
    assert answering[0][1] == ()
    assert answering[1][1] == (Turn("first", "answer to first"),)


def test_two_sessions_do_not_see_each_other(tmp_repo, answering):
    with _running(tmp_repo) as port:
        _request(port, "POST", "/chat", body={"question": "mine", "session": "s1"})
        _request(port, "POST", "/chat", body={"question": "theirs", "session": "s2"})
    assert answering[1][1] == ()


def test_a_request_with_no_session_stays_stateless(tmp_repo, answering):
    """What a widget that couldn't get storage sends, and what curl sends. Still answered, just
    with no memory."""
    with _running(tmp_repo) as port:
        _request(port, "POST", "/chat", body={"question": "first"})
        _request(port, "POST", "/chat", body={"question": "second"})
    assert [history for _q, history, _intent in answering] == [(), ()]


def test_reset_clears_the_conversation(tmp_repo, answering):
    with _running(tmp_repo) as port:
        _request(port, "POST", "/chat", body={"question": "first", "session": "s1"})
        status, _, raw = _request(port, "POST", "/chat/reset", body={"session": "s1"})
        _request(port, "POST", "/chat", body={"question": "after", "session": "s1"})
    assert (status, json.loads(raw)) == (200, {"reset": True})
    assert answering[-1][1] == ()


def test_reset_on_a_session_that_was_never_used_is_still_fine(tmp_repo, answering):
    """The widget resets optimistically — it clears its own panel first and tells the server
    after, so a reset can arrive for a conversation this process never saw."""
    with _running(tmp_repo) as port:
        assert _request(port, "POST", "/chat/reset", body={"session": "never-seen"})[0] == 200
        assert _request(port, "POST", "/chat/reset", body={})[0] == 200
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
