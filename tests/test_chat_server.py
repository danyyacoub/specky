import pytest

from specky import chat_server, db
from specky.chat_server import (
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
    monkeypatch.setattr(chat_server, "load_provider_from_toml", lambda _path: provider)

    result = answer_question(indexed_repo, "#module:billing how do refunds work?")

    assert result == {"answer": "Refunds work like this.", "sources": ["specs/billing/refund-flow.md"]}
    assert "#module:billing" not in provider.prompts[0]  # the mention never reaches the model


def test_answer_question_returns_an_html_snippet_when_the_model_sends_one(indexed_repo, monkeypatch):
    provider = FakeProvider("Here:\n```html\n<table><tr><td>1</td></tr></table>\n```")
    monkeypatch.setattr(chat_server, "load_provider_from_toml", lambda _path: provider)

    result = answer_question(indexed_repo, "refund table?")
    assert result["answer"] == "Here:"
    assert result["html_snippet"].startswith("<table>")
