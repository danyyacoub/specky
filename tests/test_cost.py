"""The prompt cache and `specky cost` — what a re-run costs, and what it says it cost.

`CachingProvider` is the only thing that writes either table, so every test here drives a real
wrapped provider against a real index rather than inserting rows by hand.
"""

from __future__ import annotations

import sqlite3

import pytest

from specky.ai_provider import PROMPT_CACHE_MAX_CHARS, CachingProvider
from specky.cost import clear_cache, report_lines, run_cost
from specky.db import connect


class Counter:
    """A provider that answers by call number, so a second identical prompt is *visibly* not it."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
        self.calls += 1
        return f"answer {self.calls}"


@pytest.fixture
def wrapped(tmp_repo):
    inner = Counter()
    return inner, CachingProvider(inner, tmp_repo, model="test-model", command="sync")


def _usage(repo) -> list[tuple]:
    conn = connect(repo)
    try:
        return conn.execute(
            "SELECT command, model, prompt_chars, response_chars, cached FROM usage "
            "ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()


def test_an_identical_prompt_never_reaches_the_provider_twice(tmp_repo, wrapped):
    inner, provider = wrapped
    assert provider.generate("why?") == "answer 1"
    assert provider.generate("why?") == "answer 1"
    assert inner.calls == 1
    assert [row[-1] for row in _usage(tmp_repo)] == [0, 1]


def test_both_calls_are_recorded_with_the_command_that_made_them(tmp_repo, wrapped):
    _, provider = wrapped
    provider.generate("why?")
    provider.generate("why?")
    assert _usage(tmp_repo) == [
        ("sync", "test-model", 4, 8, 0),
        ("sync", "test-model", 4, 8, 1),
    ]


def test_a_different_model_is_a_different_key(tmp_repo):
    """Same question, different model, different answer — a cache that ignored the model would
    hand back the old model's reply after someone switched providers."""
    inner = Counter()
    CachingProvider(inner, tmp_repo, model="cheap").generate("why?")
    CachingProvider(inner, tmp_repo, model="smart").generate("why?")
    assert inner.calls == 2


def test_a_command_provider_keys_on_its_command_line(tmp_repo):
    """`command:` providers have no model name, so the command line stands in for one."""
    inner = Counter()
    CachingProvider(inner, tmp_repo, model="command:claude").generate("why?")
    CachingProvider(inner, tmp_repo, model="command:llm -m gpt-4o").generate("why?")
    assert inner.calls == 2


def test_a_broken_index_does_not_stop_a_doc_from_being_written(tmp_repo, monkeypatch):
    """The cache is an optimization. A read-only or corrupt `.specky/` must cost speed, not docs."""
    inner = Counter()
    provider = CachingProvider(inner, tmp_repo, model="test-model")
    monkeypatch.setattr(
        "specky.ai_provider.connect", lambda _root: (_ for _ in ()).throw(sqlite3.Error("nope"))
    )
    assert provider.generate("why?") == "answer 1"


def test_the_cache_evicts_its_oldest_entries_to_stay_bounded(tmp_repo, monkeypatch):
    """The bound that matters on someone else's repo: a backfill over thousands of commits can't
    grow the index without limit, and the oldest entry is the one whose re-fetch costs least."""
    monkeypatch.setattr("specky.ai_provider.PROMPT_CACHE_MAX_CHARS", 20)

    class Big:
        def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
            return "x" * 8

    provider = CachingProvider(Big(), tmp_repo, model="test-model")
    for i in range(5):
        provider.generate(f"prompt {i}")

    conn = connect(tmp_repo)
    try:
        total = conn.execute("SELECT SUM(LENGTH(response)) FROM prompt_cache").fetchone()[0]
    finally:
        conn.close()
    assert total <= 20


def test_the_default_bound_is_generous_enough_not_to_evict_normal_use():
    """A guard on the constant itself: at ~4 KB a doc, 20 MB is thousands of them."""
    assert PROMPT_CACHE_MAX_CHARS // 4000 > 2000


# --- what `specky cost` reports


def test_cost_reports_calls_the_hit_rate_and_character_totals(tmp_repo, wrapped):
    _, provider = wrapped
    provider.generate("why?")
    provider.generate("why?")
    provider.generate("how?")

    report = run_cost(tmp_repo)
    assert len(report.groups) == 1
    group = report.groups[0]
    assert (group.command, group.model, group.calls, group.cached) == ("sync", "test-model", 3, 1)
    assert group.hit_rate == 33
    assert group.prompt_chars == 12  # "why?" twice plus "how?"
    assert group.response_chars == 24
    assert report.cache_entries == 2


def test_the_total_row_sums_every_group(tmp_repo, wrapped):
    inner, provider = wrapped
    provider.generate("why?")
    CachingProvider(inner, tmp_repo, model="test-model", command="tag").generate("how?")

    report = run_cost(tmp_repo)
    assert {g.command for g in report.groups} == {"sync", "tag"}
    assert report.total.calls == 2
    assert report.total.response_chars == sum(g.response_chars for g in report.groups)


def test_since_drops_earlier_calls(tmp_repo, wrapped):
    _, provider = wrapped
    provider.generate("why?")
    assert run_cost(tmp_repo, since="2999-01-01").groups == ()
    assert run_cost(tmp_repo, since="2000-01-01").total.calls == 1


def test_clearing_the_cache_keeps_the_record_of_what_was_spent(tmp_repo, wrapped):
    inner, provider = wrapped
    provider.generate("why?")

    assert clear_cache(tmp_repo) == 1
    assert run_cost(tmp_repo).cache_entries == 0
    assert run_cost(tmp_repo).total.calls == 1  # the usage row survives
    provider.generate("why?")
    assert inner.calls == 2  # ...and the answer has to be paid for again


def test_an_empty_log_says_so_rather_than_printing_an_empty_table(tmp_repo):
    assert "no provider calls recorded" in "\n".join(report_lines(run_cost(tmp_repo)))


def test_the_text_report_names_the_command_the_model_and_the_hit_rate(tmp_repo, wrapped):
    _, provider = wrapped
    provider.generate("why?")
    provider.generate("why?")

    text = "\n".join(report_lines(run_cost(tmp_repo)))
    assert "sync" in text and "test-model" in text
    assert "1 (50%)" in text
    assert "TOTAL" in text
    assert "--clear-cache" in text
