import asyncio
import re
from pathlib import Path

import pytest

from specky import doc_tools, mcp_server
from specky.doc_tools import (
    IMPACT_TOOLS,
    SCOPE_TOOLS,
    DocToolbox,
    StageDone,
    doc_behaviours,
    list_domains,
    read_doc,
    scope_payload,
    search_docs,
)
from specky.indexer import run_index

PANEL_DOC = """# Chat — Panel

## What It Does

A panel.

## How It Works

1. **Open** — the reader clicks the button.
2. **Ask** — the reader types a question.

## Outcomes

| Scenario | Result |
|---|---|
| Wide screen | Docked |
| n/a | — |

## Acceptance Tests

| Given | When | Then |
|---|---|---|
| Panel closed | Reader clicks | Panel opens |
| Panel open | Reader drags | Panel widens |
"""


@pytest.fixture
def docs_repo(tmp_repo, write_doc):
    write_doc("chat/panel.md", PANEL_DOC, {"type": "feature", "tags": ["chat"]})
    write_doc(
        "billing/refund-flow.md",
        "# Billing — Refund Flow\n\nA refund returns money to the customer.\n",
        {"type": "workflow"},
    )
    write_doc("history/abc123.md", "# a commit\n\nrefund refund refund\n")
    (tmp_repo / "specs" / "MODULES.md").write_text(
        "| Doc | Purpose |\n|---|---|\n| [chat/panel.md](chat/panel.md) | The panel |\n"
    )
    run_index(tmp_repo)
    return tmp_repo


# --- the plain functions ------------------------------------------------------------------------


def test_list_domains_maps_the_tree_without_history_or_index_docs(docs_repo):
    domains = {entry["domain"]: entry["docs"] for entry in list_domains(docs_repo)}

    assert sorted(domains) == ["billing", "chat"]
    (panel,) = domains["chat"]
    assert panel == {
        "path": "specs/chat/panel.md",
        "title": "Chat — Panel",
        "type": "feature",
        "purpose": "The panel",
    }


def test_search_docs_ranks_docs_and_leaves_history_out(docs_repo):
    paths = [hit["path"] for hit in search_docs(docs_repo, "refund")]
    assert paths == ["specs/billing/refund-flow.md"]


def test_search_docs_can_be_narrowed_to_one_domain(docs_repo):
    assert search_docs(docs_repo, "panel reader refund", domain="chat")[0]["domain"] == "chat"
    assert search_docs(docs_repo, "refund", domain="chat") == []


@pytest.mark.parametrize("limit", ["lots", None, [3]])
def test_a_junk_limit_falls_back_to_the_default_rather_than_failing(docs_repo, limit):
    assert search_docs(docs_repo, "refund", limit=limit)[0]["path"] == "specs/billing/refund-flow.md"


def test_read_doc_accepts_either_spelling_of_a_path(docs_repo):
    assert read_doc(docs_repo, "specs/chat/panel.md") == read_doc(docs_repo, "chat/panel.md")
    assert read_doc(docs_repo, "chat/panel.md").startswith("---\ntype: feature")


@pytest.mark.parametrize("path", ["../README.md", "README.md", "chat/../../README.md"])
def test_read_doc_refuses_anything_outside_the_docs_tree(docs_repo, path):
    with pytest.raises(ValueError, match="not under specs/"):
        read_doc(docs_repo, path)


def test_an_absolute_path_is_looked_up_inside_the_docs_tree_not_outside_it(docs_repo):
    """The leading slash is stripped, so `/etc/passwd` names `specs/etc/passwd` — which isn't there."""
    with pytest.raises(ValueError, match="no doc at etc/passwd"):
        read_doc(docs_repo, "/etc/passwd")


def test_read_doc_says_when_a_doc_does_not_exist(docs_repo):
    with pytest.raises(ValueError, match="no doc at"):
        read_doc(docs_repo, "chat/nope.md")


def test_doc_behaviours_numbers_every_stated_behaviour(docs_repo):
    rows = doc_behaviours(docs_repo, "specs/chat/panel.md")

    assert [row["id"] for row in rows] == ["STEP-1", "STEP-2", "OUT-1", "AT-1", "AT-2"]
    assert rows[0]["text"] == "Open — the reader clicks the button."
    assert rows[2]["fields"] == {"Scenario": "Wide screen", "Result": "Docked"}
    assert rows[3]["text"] == "Given: Panel closed · When: Reader clicks · Then: Panel opens"


def test_a_placeholder_row_is_not_a_behaviour(docs_repo):
    assert "OUT-2" not in {row["id"] for row in doc_behaviours(docs_repo, "specs/chat/panel.md")}


# --- the terminal tools' validation --------------------------------------------------------------


def test_an_existing_doc_decides_the_scope_whatever_else_was_said(docs_repo):
    scope = scope_payload(docs_repo, "search", "whatever", "", "chat/panel.md", "it's the panel")
    assert scope == {
        "domain": "chat",
        "topic": "panel",
        "type": "feature",  # read from the doc's own frontmatter
        "existing": "specs/chat/panel.md",
        "path": "specs/chat/panel.md",
        "reason": "it's the panel",
    }


def test_a_new_doc_scope_is_kebab_cased_and_its_path_rebuilt(docs_repo):
    scope = scope_payload(docs_repo, "Chat", "Retry Logic", "workflow")
    assert (scope["domain"], scope["topic"], scope["type"]) == ("chat", "retry-logic", "workflow")
    assert scope["path"] == "specs/chat/retry-logic.md"
    assert scope["existing"] is None


@pytest.mark.parametrize(
    "args,complaint",
    [
        (("chat", "x", "feature", "chat/missing.md"), "No doc at"),
        (("chat", "x", "feature", "../README.md"), "No doc at"),
        (("chat", "", "feature"), "both a domain and a topic"),
        (("chat", "README", "feature"), "never a topic"),
    ],
)
def test_a_bad_scope_is_a_complaint_the_model_can_fix(docs_repo, args, complaint):
    assert complaint in scope_payload(docs_repo, *args)


def test_a_missing_doc_complaint_points_at_the_map_not_a_tool_the_model_lacks(docs_repo):
    complaint = scope_payload(docs_repo, "chat", "x", "feature", "chat/missing.md")
    assert "map of existing docs" in complaint and "list_domains" not in complaint


def test_set_scope_ends_the_stage_with_a_validated_scope(docs_repo):
    toolbox = DocToolbox(docs_repo, SCOPE_TOOLS)
    with pytest.raises(StageDone) as done:
        toolbox.invoke("set_scope", {"domain": "chat", "topic": "retry", "type": "feature"})
    assert done.value.kind == "scope"
    assert done.value.payload["path"] == "specs/chat/retry.md"


def test_a_rejected_scope_does_not_end_the_stage(docs_repo):
    toolbox = DocToolbox(docs_repo, SCOPE_TOOLS)
    result = toolbox.invoke(
        "set_scope", {"domain": "chat", "topic": "x", "type": "feature", "existing_doc": "no.md"}
    )
    assert "No doc at no.md" in result


def test_ask_user_carries_clickable_options(docs_repo):
    toolbox = DocToolbox(docs_repo, SCOPE_TOOLS)
    options = [{"label": "Chat", "domain": "chat"}, "Somewhere else", {"nope": 1}] + [
        {"label": f"extra {i}"} for i in range(10)
    ]
    with pytest.raises(StageDone) as done:
        toolbox.invoke("ask_user", {"question": "Which module?", "options": options})
    payload = done.value.payload
    assert payload["text"] == "Which module?"
    assert payload["options"][:2] == [{"label": "Chat", "domain": "chat"}, {"label": "Somewhere else"}]
    assert len(payload["options"]) == doc_tools.MAX_OPTIONS


def test_submit_impact_coerces_kinds_rather_than_refusing_them(docs_repo):
    toolbox = DocToolbox(docs_repo, IMPACT_TOOLS)
    with pytest.raises(StageDone) as done:
        toolbox.invoke(
            "submit_impact",
            {
                "summary": "Retries.",
                "changes": [{"section": "Outcomes", "kind": "update", "summary": "Adds a row."}],
                "behaviours": [
                    {"ref": "AT-1", "behaviour": "Open", "today": "a", "after": "b", "kind": "odd"}
                ],
            },
        )
    impact = done.value.payload
    assert impact["changes"][0]["kind"] == "modify"
    assert impact["behaviours"][0]["kind"] == "changed"


def test_an_empty_impact_is_sent_back(docs_repo):
    toolbox = DocToolbox(docs_repo, IMPACT_TOOLS)
    assert "at least one change" in toolbox.invoke("submit_impact", {"summary": "nothing"})


# --- the toolbox ---------------------------------------------------------------------------------


def test_each_stage_only_gets_its_own_tools(docs_repo):
    assert [t.name for t in DocToolbox(docs_repo, SCOPE_TOOLS).tools()] == list(SCOPE_TOOLS)
    impact = DocToolbox(docs_repo, IMPACT_TOOLS)
    assert impact.invoke("set_scope", {"domain": "x"}) == "No tool named 'set_scope' on this stage."


def test_read_doc_records_the_doc_as_a_source_in_repo_relative_form(docs_repo):
    toolbox = DocToolbox(docs_repo, SCOPE_TOOLS)
    toolbox.invoke("read_doc", {"path": "chat/panel.md"})
    assert toolbox.read == ["specs/chat/panel.md"]


def test_a_spent_budget_stops_every_reading_tool(docs_repo, monkeypatch):
    monkeypatch.setattr(doc_tools, "DRAFT_TOOL_BUDGET", 10)
    toolbox = DocToolbox(docs_repo, IMPACT_TOOLS)
    toolbox.invoke("read_doc", {"path": "chat/panel.md"})  # overshoots, and still returns in full
    for name, args in [
        ("read_doc", {"path": "chat/panel.md"}),
        ("doc_behaviours", {"path": "chat/panel.md"}),
        ("search_docs", {"query": "panel"}),
        ("search_history", {"query": "panel"}),
    ]:
        assert toolbox.invoke(name, args).startswith("Tool budget spent")


def test_a_bad_argument_is_reported_not_raised(docs_repo):
    toolbox = DocToolbox(docs_repo, SCOPE_TOOLS)
    assert toolbox.invoke("read_doc", {"wrong": "x"}).startswith("read_doc:")


# --- the MCP surface ------------------------------------------------------------------------------


def test_the_mcp_tools_are_the_same_functions(docs_repo, monkeypatch):
    monkeypatch.setattr(mcp_server, "repo_root", lambda: docs_repo)

    assert mcp_server.list_domains() == list_domains(docs_repo)
    assert mcp_server.doc_behaviours("specs/chat/panel.md")[0]["id"] == "STEP-1"
    assert mcp_server.search_docs("refund")[0]["path"] == "specs/billing/refund-flow.md"
    assert mcp_server.read_doc("chat/panel.md").startswith("---")
    table = mcp_server.render_acceptance_table(
        [{"scenario": "Open", "given": "closed", "when": "click", "then": "opens | fast"}]
    )
    assert table.splitlines()[2] == r"| Open | closed | click | opens \| fast |"


def test_the_mcp_prompts_spell_out_both_workflows():
    draft = mcp_server.draft_spec("retry failed requests")
    assert "retry failed requests" in draft
    for stage in ("Stage 1 — Scope", "Stage 2 — Impact", "Stage 3 — Acceptance tests", "Stage 4"):
        assert stage in draft
    assert "APPROVE" in draft and "render_acceptance_table" in draft
    assert "(approved table)" not in draft  # that placeholder is the panel's splice, not the host's

    explore = mcp_server.explore("how do refunds work?")
    assert "<!-- more -->" in explore and "search_docs" in explore


def test_what_tells_a_host_when_to_use_specky_names_only_real_tools():
    # The server instructions and the explore-docs skill both steer a host's model to tools by
    # name. A renamed tool must break this, not leave the model calling one that isn't there.
    tools = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    skill = (Path(__file__).parent.parent / "skills" / "explore-docs" / "SKILL.md").read_text()

    in_instructions = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", mcp_server.INSTRUCTIONS))
    in_skill = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", skill))
    assert mcp_server.mcp.instructions == mcp_server.INSTRUCTIONS
    assert {"search_docs", "read_doc", "doc_behaviours"} <= in_instructions & in_skill
    assert in_instructions <= tools
    assert in_skill <= tools
