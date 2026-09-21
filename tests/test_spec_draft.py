import json

import pytest

from specky import spec_draft, testgen
from specky.doc_tools import IMPACT_TOOLS, SCOPE_TOOLS
from specky.indexer import run_index
from specky.spec_draft import DraftError, DraftState, acceptance_table

from conftest import FakeProvider

PANEL_DOC = """# Chat — Panel

## What It Does

A panel for asking questions about the docs.

## How It Works

1. **Open** — the reader clicks the button.
2. **Ask** — the reader types a question.

## Outcomes

| Scenario | Result |
|---|---|
| Wide screen | Docked |

## Acceptance Tests

| Given | When | Then |
|---|---|---|
| Panel closed | Reader clicks | Panel opens |
"""

IMPACT = {
    "summary": "A failed question is retried once.",
    "changes": [{"section": "How It Works", "kind": "modify", "summary": "Adds a retry step."}],
    "behaviours": [
        {
            "ref": "STEP-2",
            "behaviour": "Ask",
            "today": "A failed question shows an error.",
            "after": "A failed question is retried once, then shows an error.",
            "kind": "changed",
        }
    ],
}

TESTS = [
    {
        "scenario": "Retry once",
        "given": "a question failed",
        "when": "the reader waits",
        "then": "it is retried once",
        "covers": "STEP-2",
    }
]

FINAL_DOC = """# Chat — Panel

## What It Does

A panel for asking questions about the docs, which retries a failed question once.

## How It Works

1. **Open** — the reader clicks the button.
2. **Ask** — the reader types a question; a failure is retried once.

## Outcomes

| Scenario | Result |
|---|---|
| Wide screen | Docked |

## Acceptance Tests

| Given | When | Then |
|---|---|---|
| the model | made | this row up |
"""


class DraftProvider:
    """One scripted tool conversation per `converse` call, one canned reply per `generate` call.

    A draft runs several stages, each its own conversation, so a single script replayed on every
    call (conftest.ToolProvider) can't drive one.
    """

    def __init__(self, conversations=(), replies=()):
        self.conversations = list(conversations)
        self.replies = list(replies)
        self.converse_prompts: list[str] = []
        self.generate_prompts: list[str] = []
        self.tool_names: list[list[str]] = []
        self.tasks: list[str] = []
        self.final_tools: list[str] = []

    def converse(self, prompt, *, prefix="", tools, invoke, task="", max_turns=8, final_tool="", on_turn=None):
        self.converse_prompts.append(prefix + prompt)
        self.tool_names.append([tool.name for tool in tools])
        self.tasks.append(task)
        self.final_tools.append(final_tool)
        if not self.conversations:
            raise AssertionError("DraftProvider ran out of scripted conversations")
        for turn in self.conversations.pop(0)[:max_turns]:
            for name, arguments in turn:
                invoke(name, arguments)
        return ""

    def generate(self, prompt, *, prefix="", task=""):
        self.generate_prompts.append(prefix + prompt)
        self.tasks.append(task)
        if not self.replies:
            raise AssertionError("DraftProvider ran out of canned replies")
        return self.replies.pop(0)


def _scope_to_panel():
    return [
        [("search_docs", {"query": "panel question"}), ("read_doc", {"path": "chat/panel.md"})],
        [("set_scope", {"domain": "chat", "topic": "panel", "type": "feature", "existing_doc": "specs/chat/panel.md"})],
    ]


def _impact():
    return [[("submit_impact", IMPACT)]]


def _browser(result: dict) -> dict:
    """The state as the panel sends it back: through JSON and sessionStorage."""
    return json.loads(json.dumps(result["draft"]))


@pytest.fixture
def repo(tmp_repo, write_doc):
    write_doc("chat/panel.md", PANEL_DOC, {"type": "feature", "tags": ["chat"]})
    write_doc(
        "billing/refund-flow.md",
        "# Billing — Refund Flow\n\nA refund returns money to the customer.\n",
        {"type": "workflow"},
    )
    run_index(tmp_repo)
    return tmp_repo


# --- scope and impact ----------------------------------------------------------------------------


def test_a_draft_places_the_change_and_works_out_what_it_changes(repo):
    provider = DraftProvider([_scope_to_panel(), _impact()])

    result = spec_draft.start(repo, provider, "draft a spec for retrying failed questions in the panel")

    draft = result["draft"]
    assert draft["stage"] == "impact"
    assert draft["scope"]["existing"] == "specs/chat/panel.md"
    assert draft["impact"]["behaviours"][0]["ref"] == "STEP-2"
    assert result["sources"] == ["specs/chat/panel.md"]
    # Scope saw the map of the tree; impact saw the target doc whole, with numbered behaviours.
    scope_prompt, impact_prompt = provider.converse_prompts
    assert "specs/billing/refund-flow.md" in scope_prompt
    assert "--- The target doc now, in full (specs/chat/panel.md) ---" in impact_prompt
    assert "STEP-2  Ask — the reader types a question." in impact_prompt
    assert provider.tool_names == [list(SCOPE_TOOLS), list(IMPACT_TOOLS)]
    assert provider.final_tools == ["set_scope", "submit_impact"]
    assert set(provider.tasks) == {"draft"}


def test_a_feature_mention_skips_the_scope_stage(repo):
    provider = DraftProvider([_impact()])

    result = spec_draft.start(repo, provider, "add retries", mention={"kind": "feature", "value": "panel"})

    assert result["draft"]["scope"]["path"] == "specs/chat/panel.md"
    assert len(provider.converse_prompts) == 1
    assert "The target doc now" in provider.converse_prompts[0]


def test_a_feature_mention_matching_nothing_is_a_hint_not_a_scope(repo):
    provider = DraftProvider([_scope_to_panel(), _impact()])

    spec_draft.start(repo, provider, "add retries", mention={"kind": "feature", "value": "nope"})

    assert "#feature:nope" in provider.converse_prompts[0]


def test_a_module_mention_steers_the_scope(repo):
    provider = DraftProvider([_scope_to_panel(), _impact()])

    spec_draft.start(repo, provider, "add retries", mention={"kind": "module", "value": "chat"})

    assert "The reader scoped this to the `chat` module." in provider.converse_prompts[0]


def test_an_unsure_model_asks_the_reader(repo):
    ask = {
        "question": "Which doc should this change?",
        "options": [
            {"label": "The chat panel", "existing_doc": "specs/chat/panel.md"},
            {"label": "Something new"},
        ],
    }
    provider = DraftProvider([[[("ask_user", ask)]]])

    result = spec_draft.start(repo, provider, "retries")

    draft = result["draft"]
    assert draft["stage"] == "question"
    assert draft["question"]["text"] == "Which doc should this change?"
    assert draft["question"]["resume"] == "scope"
    assert draft["scope"] is None


def test_an_option_naming_a_place_settles_the_scope_without_the_model(repo):
    asking = DraftProvider(
        [[[("ask_user", {"question": "Where?", "options": [{"label": "Chat panel", "existing_doc": "specs/chat/panel.md"}]})]]]
    )
    state = _browser(spec_draft.start(repo, asking, "retries"))
    provider = DraftProvider([_impact()])

    result = spec_draft.act(repo, provider, "choose", state, option=0)

    draft = result["draft"]
    assert draft["stage"] == "impact"
    assert draft["scope"]["existing"] == "specs/chat/panel.md"
    assert draft["clarifications"] == [{"q": "Where?", "a": "Chat panel"}]
    assert len(provider.converse_prompts) == 1  # impact only — the click placed it


def test_a_placeless_option_is_answered_as_if_typed(repo):
    asking = DraftProvider([[[("ask_user", {"question": "New or existing?", "options": ["Make it new"]})]]])
    state = _browser(spec_draft.start(repo, asking, "retries"))
    provider = DraftProvider([_scope_to_panel(), _impact()])

    spec_draft.act(repo, provider, "choose", state, option=0)

    assert "Q: New or existing?\n  A: Make it new" in provider.converse_prompts[0]


def test_an_option_that_is_not_offered_is_refused(repo):
    asking = DraftProvider([[[("ask_user", {"question": "Where?", "options": ["here"]})]]])
    state = _browser(spec_draft.start(repo, asking, "retries"))

    with pytest.raises(DraftError, match="isn't one of the choices"):
        spec_draft.act(repo, DraftProvider(), "choose", state, option=5)


def test_a_typed_answer_reruns_the_stage_that_asked(repo):
    asking = DraftProvider([[[("ask_user", {"question": "Which module?"})]]])
    state = _browser(spec_draft.start(repo, asking, "retries"))
    provider = DraftProvider([_scope_to_panel(), _impact()])

    result = spec_draft.act(repo, provider, "reply", state, reply="the chat one")

    assert result["draft"]["stage"] == "impact"
    assert "Q: Which module?\n  A: the chat one" in provider.converse_prompts[0]


def test_an_impact_stage_question_resumes_the_impact_not_the_scope(repo):
    provider = DraftProvider([_scope_to_panel(), [[("ask_user", {"question": "Retry how often?"})]]])
    state = _browser(spec_draft.start(repo, provider, "retries"))
    assert state["question"]["resume"] == "impact"
    resumed = DraftProvider([_impact()])

    result = spec_draft.act(repo, resumed, "reply", state, reply="once")

    assert result["draft"]["stage"] == "impact"
    assert "The target doc now" in resumed.converse_prompts[0]


def test_an_impact_question_cannot_move_the_draft(repo):
    """Seen against a real model: an impact-stage question whose options each named a doc. Clicking
    one answers the question; the target stays where the scope stage put it."""
    ask = {
        "question": "What should the reader see when the retry fails too?",
        "options": [{"label": "A retryable error", "existing_doc": "specs/billing/refund-flow.md"}],
    }
    state = _browser(
        spec_draft.start(repo, DraftProvider([_scope_to_panel(), [[("ask_user", ask)]]]), "retries")
    )
    assert state["question"]["options"] == [{"label": "A retryable error"}]
    state["question"]["options"][0]["existing_doc"] = "specs/billing/refund-flow.md"  # tampered
    provider = DraftProvider([_impact()])

    result = spec_draft.act(repo, provider, "choose", state, option=0)

    assert result["draft"]["scope"]["existing"] == "specs/chat/panel.md"
    assert "A: A retryable error" in provider.converse_prompts[0]


def test_a_model_that_never_places_the_change_gets_the_reader_asked(repo):
    provider = DraftProvider([[[("search_docs", {"query": "refund"})]]])

    result = spec_draft.start(repo, provider, "refund retries")

    question = result["draft"]["question"]
    assert question["text"] == "Which module does this change belong to?"
    assert question["options"][0] == {"label": "billing", "domain": "billing"}


def test_a_model_that_never_finishes_the_impact_is_an_error_the_reader_sees(repo):
    provider = DraftProvider([_scope_to_panel(), [[("read_doc", {"path": "chat/panel.md"})]]])

    with pytest.raises(DraftError, match="without working out what this changes"):
        spec_draft.start(repo, provider, "retries")


def test_a_correction_to_the_impact_revises_it(repo):
    state = _browser(spec_draft.start(repo, DraftProvider([_scope_to_panel(), _impact()]), "retries"))
    provider = DraftProvider([_impact()])

    spec_draft.act(repo, provider, "reply", state, reply="retry twice, not once")

    prompt = provider.converse_prompts[0]
    assert "Correction to the impact" in prompt and "retry twice, not once" in prompt
    assert "Your previous impact" in prompt and "A failed question is retried once." in prompt


def test_rescope_starts_the_placement_over(repo):
    state = _browser(spec_draft.start(repo, DraftProvider([_scope_to_panel(), _impact()]), "retries"))
    provider = DraftProvider([_scope_to_panel(), _impact()])

    result = spec_draft.act(repo, provider, "rescope", state)

    assert len(provider.converse_prompts) == 2
    assert result["draft"]["stage"] == "impact"


# --- acceptance tests and the final draft --------------------------------------------------------


def _at_impact(repo):
    return _browser(spec_draft.start(repo, DraftProvider([_scope_to_panel(), _impact()]), "retries"))


def _at_acceptance(repo):
    provider = DraftProvider(replies=[json.dumps({"tests": TESTS})])
    return _browser(spec_draft.act(repo, provider, "confirm", _at_impact(repo)))


def test_confirming_the_impact_writes_acceptance_tests(repo):
    provider = DraftProvider(replies=[json.dumps({"tests": TESTS + [{"given": "no then"}]})])

    result = spec_draft.act(repo, provider, "confirm", _at_impact(repo))

    assert result["draft"]["stage"] == "acceptance"
    assert result["draft"]["tests"] == TESTS  # the row with no `then` asserted nothing
    prompt = provider.generate_prompts[0]
    assert "AT-1  Given: Panel closed · When: Reader clicks · Then: Panel opens" in prompt
    assert "A failed question is retried once." in prompt


def test_no_acceptance_tests_is_an_error(repo):
    provider = DraftProvider(replies=["I think it's fine."])
    with pytest.raises(DraftError, match="didn't return any acceptance tests"):
        spec_draft.act(repo, provider, "confirm", _at_impact(repo))


def test_a_correction_to_the_tests_revises_them(repo):
    provider = DraftProvider(replies=[json.dumps({"tests": TESTS})])

    spec_draft.act(repo, provider, "reply", _at_acceptance(repo), reply="add a row for two failures")

    prompt = provider.generate_prompts[0]
    assert "add a row for two failures" in prompt and "Your previous rows" in prompt


def test_the_final_draft_carries_the_approved_table_not_the_models(repo):
    provider = DraftProvider(replies=[FINAL_DOC])

    result = spec_draft.act(repo, provider, "approve", _at_acceptance(repo))

    assert result["draft"]["stage"] == "final"
    answer = result["answer"]
    assert "| Retry once | a question failed | the reader waits | it is retried once |" in answer
    assert "this row up" not in answer
    assert answer.startswith("---\ntype: feature\ntags: [chat]")  # the doc's own frontmatter kept
    assert result["path"] == "specs/chat/panel.md"
    assert "+2. **Ask** — the reader types a question; a failure is retried once." in result["diff"]
    assert "<h1>" in result["answer_html"] or "Chat — Panel" in result["answer_html"]
    assert testgen.parse_scenarios(answer)[0].then == "it is retried once"
    assert not (repo / ".specky" / "pending").exists()  # nothing written anywhere


def test_a_final_draft_that_drops_a_section_is_flagged(repo):
    gutted = FINAL_DOC.replace("## Outcomes\n\n| Scenario | Result |\n|---|---|\n| Wide screen | Docked |\n", "")
    provider = DraftProvider(replies=[gutted])

    result = spec_draft.act(repo, provider, "approve", _at_acceptance(repo))

    assert any("drops `## Outcomes`" in warning for warning in result["warnings"])


def test_a_new_doc_gets_its_type_and_no_diff(repo):
    scope = [[("set_scope", {"domain": "chat", "topic": "retry-flow", "type": "workflow"})]]
    state = _browser(spec_draft.start(repo, DraftProvider([scope, _impact()]), "retries"))
    state = _browser(
        spec_draft.act(repo, DraftProvider(replies=[json.dumps({"tests": TESTS})]), "confirm", state)
    )
    provider = DraftProvider(replies=["```markdown\n# Chat — Retry Flow\n\n## What It Does\n\nRetries.\n```"])

    result = spec_draft.act(repo, provider, "approve", state)

    assert result["answer"].startswith("---\ntype: workflow\n---\n\n# Chat — Retry Flow")
    assert "## Acceptance Tests" in result["answer"]
    assert (result["diff"], result["warnings"]) == ("", [])
    assert "## Edge Cases" in provider.generate_prompts[0]  # the workflow template was asked for


def test_approving_before_there_are_tests_is_refused(repo):
    with pytest.raises(DraftError, match="no acceptance tests to approve"):
        spec_draft.act(repo, DraftProvider(), "approve", _at_impact(repo))


def test_an_unknown_action_is_refused(repo):
    with pytest.raises(DraftError, match="Unknown draft action"):
        spec_draft.act(repo, DraftProvider(), "explode", _at_impact(repo))


# --- a provider with no tool channel ------------------------------------------------------------


def test_a_provider_without_tools_runs_each_stage_as_one_call(repo):
    provider = FakeProvider(
        [
            json.dumps({"scope": {"domain": "chat", "topic": "panel", "type": "feature", "existing_doc": "specs/chat/panel.md"}}),
            json.dumps({"impact": IMPACT}),
        ]
    )

    result = spec_draft.start(repo, provider, "retries in the panel")

    assert result["draft"]["stage"] == "impact"
    assert result["draft"]["scope"]["existing"] == "specs/chat/panel.md"
    assert "--- Docs that match the request ---" in provider.prompts[0]
    assert '{"scope":' in provider.prompts[0]


def test_only_a_model_with_tools_is_told_to_finish_with_a_tool(repo):
    """The degraded call is asked for JSON; telling it to call a final tool it doesn't have would
    contradict that."""
    tooled = DraftProvider([_scope_to_panel(), _impact()])
    spec_draft.start(repo, tooled, "retries in the panel")
    assert all(spec_draft.TURN_BUDGET in prompt for prompt in tooled.converse_prompts)

    degraded = FakeProvider(
        [
            json.dumps({"scope": {"domain": "chat", "topic": "panel", "existing_doc": "specs/chat/panel.md"}}),
            json.dumps({"impact": IMPACT}),
        ]
    )
    spec_draft.start(repo, degraded, "retries in the panel")
    assert not any(spec_draft.TURN_BUDGET in prompt for prompt in degraded.prompts)


def test_an_unreadable_degraded_answer_asks_the_reader(repo):
    result = spec_draft.start(repo, FakeProvider("no json here"), "refund retries")
    assert result["draft"]["stage"] == "question"


def test_a_degraded_answer_that_fails_validation_is_not_trusted(repo):
    bad = json.dumps({"scope": {"domain": "chat", "topic": "x", "existing_doc": "../README.md"}})
    result = spec_draft.start(repo, FakeProvider(bad), "refund retries")
    assert result["draft"]["stage"] == "question"
    assert result["draft"]["scope"] is None


# --- the state the browser holds -----------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "state", [], {"stage": "impact"}])
def test_a_missing_or_shapeless_state_is_refused(repo, raw):
    with pytest.raises(DraftError):
        DraftState.from_payload(repo, raw)


def test_a_state_that_does_not_hang_together_is_refused(repo):
    state = _at_impact(repo)
    state["stage"] = "acceptance"  # but it has no tests
    with pytest.raises(DraftError, match="doesn't hang together"):
        DraftState.from_payload(repo, state)


def test_a_tampered_path_is_rebuilt_from_its_parts(repo):
    state = _at_impact(repo)
    state["scope"]["path"] = "../../etc/passwd"
    assert DraftState.from_payload(repo, state).scope["path"] == "specs/chat/panel.md"


def test_a_target_outside_the_docs_tree_is_refused(repo):
    state = _at_impact(repo)
    state["scope"]["existing"] = "../README.md"
    with pytest.raises(DraftError, match="no longer checks out"):
        DraftState.from_payload(repo, state)


def test_everything_the_browser_sends_is_capped(repo):
    state = _at_acceptance(repo)
    state["request"] = "x" * 50_000
    state["tests"] = TESTS * 100
    state["clarifications"] = [{"q": "q", "a": "a"}] * 50
    state["sources"] = ["s"] * 100

    parsed = DraftState.from_payload(repo, state)

    assert len(parsed.request) <= spec_draft.REQUEST_CHARS + 1
    assert len(parsed.tests) == spec_draft.MAX_TESTS
    assert len(parsed.clarifications) == spec_draft.MAX_CLARIFICATIONS
    assert len(parsed.sources) == spec_draft.MAX_SOURCES


# --- the table and the shared rules ---------------------------------------------------------------


def test_the_acceptance_table_escapes_pipes_and_reads_back_through_specky_tests():
    table = acceptance_table([{"scenario": "A|B", "given": "g", "when": "w", "then": "t | u"}])
    doc = f"# X\n\n## Acceptance Tests\n\n{table}\n"

    (scenario,) = testgen.parse_scenarios(doc)
    assert (scenario.name, scenario.then) == ("A|B", "t | u")


def test_the_panel_and_the_mcp_prompt_share_their_rules():
    workflow = spec_draft.workflow_prompt("x")
    for rules in (spec_draft.SCOPE_RULES, spec_draft.IMPACT_RULES, spec_draft.ACCEPTANCE_RULES, spec_draft.FINAL_RULES):
        assert rules in workflow
    assert spec_draft.SCOPE_RULES in spec_draft.SCOPE_INSTRUCTIONS
    assert spec_draft.IMPACT_RULES in spec_draft.IMPACT_INSTRUCTIONS
    assert spec_draft.ACCEPTANCE_RULES in spec_draft.ACCEPTANCE_INSTRUCTIONS
    assert spec_draft.FINAL_RULES in spec_draft.FINAL_INSTRUCTIONS
