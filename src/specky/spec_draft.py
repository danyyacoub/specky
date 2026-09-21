"""The Spec Assistant's draft-spec workflow: Scope → Impact → Acceptance tests → Final draft.

The panel used to answer "draft a spec for X" in one call, and the draft was only as good as the
model's first guess about three things nobody had settled: *where* the doc goes (which domain, and
whether a doc on this already exists), *what* the change does to what the docs already promise, and
*how* anyone would know it was done. So a draft is now a short conversation that settles them in
that order, and stops to ask the reader wherever a guess would be expensive:

1. **Scope** — the model searches the docs (`doc_tools.DocToolbox`) and either places the change
   (`set_scope`) or asks the reader to choose (`ask_user`). A `#feature:` mention skips this: the
   reader already said which doc.
2. **Impact** — with the target doc and its numbered behaviours in hand, the model lists the
   sections that change and the behaviours that change, each with what happens today and after.
   The reader confirms, or types a correction and it runs again.
3. **Acceptance tests** — Given/When/Then rows covering every changed behaviour, for the reader to
   approve or correct.
4. **Final draft** — the whole doc, with the *approved* table spliced in by specky rather than
   retyped by the model, and a diff against the doc it would replace.

Nothing is written to disk. The server that runs this is `specky serve`, reachable from a browser,
and the draft is for a reader to copy — the same "no write-to-docs endpoint" line the panel has
always held.

The state of a draft lives in the reader's tab, not in this process: each response carries it
(`DraftState.to_payload`), the panel keeps it in sessionStorage, and sends it back with the next
action. So a draft survives a `specky serve` restart and a page navigation, and this module holds no
per-reader state at all. The price is that the state arrives from a client, so `from_payload`
re-validates every field of it — the same validators the terminal tools use — and never trusts a
path it could rebuild.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from specky import frontmatter, paths
from specky.ai_provider import Provider, ToolLoopUnsupported, supports_tools
from specky.doc_tools import (
    IMPACT_TOOLS,
    QUESTION_WORDS,
    SCOPE_TOOLS,
    DocToolbox,
    StageDone,
    behaviours_text,
    clip,
    doc_behaviours,
    domains_text,
    impact_payload,
    kebab,
    list_domains,
    question_payload,
    scope_payload,
    search_docs,
)
from specky.generator import (
    doc_style,
    leading_json_object,
    lost_content,
    merge_sections,
    repeated_sections,
    strip_code_fence,
)

# Turns one tool-using stage may take. Fewer than `specky document`'s sixteen: a stage reads docs,
# not a codebase, and it runs while a reader watches a spinner.
DRAFT_MAX_TURNS = 8

STAGE_QUESTION = "question"
STAGE_IMPACT = "impact"
STAGE_ACCEPTANCE = "acceptance"
STAGE_FINAL = "final"
STAGES = (STAGE_QUESTION, STAGE_IMPACT, STAGE_ACCEPTANCE, STAGE_FINAL)
# What a question resumes once answered.
RESUMABLE = ("scope", "impact")

ACTIONS = ("reply", "choose", "confirm", "approve", "rescope")

# Bounds on what round-trips through the browser. Every one of these is replayed into each later
# prompt of the draft, so they bound cost as much as they bound the payload.
REQUEST_CHARS = 2_000
REPLY_CHARS = 1_000
MAX_CLARIFICATIONS = 8
MAX_TESTS = 30
MAX_SOURCES = 20
DOMAIN_MAP_CHARS = 12_000
DIFF_CHARS = 20_000

# Told to a model that has tools, on the tool path only (`_run_tools`) — a degraded single call is
# asked for JSON instead, and telling it to finish with a tool it doesn't have contradicts that.
TURN_BUDGET = (
    f"You have at most {DRAFT_MAX_TURNS} turns. Read what you need, then finish with the stage's "
    "final tool — a stage that never finishes produces nothing."
)


class DraftError(RuntimeError):
    """Something the reader should be told, in words they can act on."""


# --- the rules, shared by the panel's prompts and the MCP `draft_spec` prompt ---------------------
#
# Written without tool names on purpose: the panel's model has `set_scope`/`submit_impact`, an MCP
# host has `search_docs`/`render_acceptance_table` and a conversation with the reader instead. The
# rules are the part that must not drift between the two, so they are the part written once.

CONVENTION = (
    "Docs live at `specs/<domain>/<topic>.md`: a domain is a module of the product (the folder), "
    "a topic is one feature or workflow (the file, kebab-case, never README). A feature is a "
    "bounded capability; a workflow is a multi-step process with an order and an outcome per step."
)

SCOPE_RULES = """Decide where the change belongs before anything is drafted:
- The domain: an existing one wherever one fits; a new one only when nothing existing is about this.
- Whether an existing doc already covers the subject. If one does, the change UPDATES that doc — a
  second doc on the same subject is a defect. Otherwise it is a new topic in that domain.
- Whether it is a feature or a workflow.
Check with a search before deciding. When two or more places are genuinely plausible, or the request
is too vague to place, ask the reader one short question with 2-5 options, each naming the place it
stands for. Never guess between plausible places: asking costs one click, a doc in the wrong module
costs a reader who never finds it."""

IMPACT_RULES = """Work out what the change does to what the docs already promise:
- summary: one or two sentences — what the reader gets once this is done.
- changes: each section of the target doc that changes — its heading, add / modify / remove, and one
  sentence on what changes. For a new doc, the sections it will have (all `add`).
- behaviours: each behaviour that changes, appears or disappears — the row id it changes (e.g. AT-3,
  or `<doc path>#OUT-2` for a row in another doc; empty for a new behaviour), a short name, what
  happens TODAY according to the docs (`nothing — new` for a new one), what will happen AFTER, and
  changed / new / removed.
Ground "today" in what the docs actually say, including related docs this change reaches into. List
only what changes — never a behaviour that stays the same — and never invent a requirement the
reader didn't ask for. If the request leaves a behaviour genuinely open (two reasonable readings
that would produce different tests), ask the reader one short question with 2-4 options instead."""

ACCEPTANCE_RULES = """Write the acceptance tests for the change as Given/When/Then rows:
- At least one row per changed or new behaviour in the impact, and one per edge case it names.
- `then` is observable — what the reader or the system can see — never "works correctly".
- Keep the target doc's existing acceptance rows that still hold, word for word. Rewrite the rows
  whose behaviour changes; drop the rows for removed behaviours.
- Each row says what it covers: the behaviour's row id, or its short name for a new one.
- Plain language a non-technical stakeholder can follow; no code."""

FINAL_RULES = """Write the complete doc for the change, in markdown, starting at its `# ` heading,
with no frontmatter.
- Updating an existing doc: return the WHOLE doc. Keep every section and every sentence that is
  still true, and change only what the impact lists — dropping existing content is a defect.
- Anything the docs and the reader's answers don't settle is written as `TBD — not in the docs` in
  place, never invented and never quietly left out.
- Never emit raw HTML."""

# How the approved table reaches the doc differs by who writes it. The panel's model is told to
# leave a placeholder, because specky splices the table in itself (`_run_final`) and a model
# retyping thirty rows is thirty chances to reword one; an MCP host has no splice step, so it pastes
# the table `render_acceptance_table` gave it.
_PANEL_TABLE_RULE = (
    "- Under `## Acceptance Tests` write the single line `(approved table)`: the table the reader "
    "approved is inserted there word for word."
)
_MCP_TABLE_RULE = (
    "- Under `## Acceptance Tests` put the approved table exactly as render_acceptance_table "
    "returned it."
)


def _join(*parts: str) -> str:
    return "\n\n".join(part.strip() for part in parts if part.strip())


_PANEL_ROLE = (
    "You are the Spec Assistant for this codebase's documentation, helping a reader draft a change "
    "to it. You read the project's docs and their git history — never its source code."
)

SCOPE_INSTRUCTIONS = _join(
    _PANEL_ROLE,
    CONVENTION,
    SCOPE_RULES,
    "The map of every doc that exists is at the end of these instructions. Use search_docs and "
    "read_doc to check what an existing doc covers. Finish by calling set_scope (pass existing_doc "
    "when a doc already covers the subject), or ask_user when you would otherwise be guessing — "
    "give each option the domain and existing_doc or topic it stands for.",
)

IMPACT_INSTRUCTIONS = _join(
    _PANEL_ROLE,
    CONVENTION,
    IMPACT_RULES,
    "The target doc and its numbered behaviours are given below when it exists. Use search_docs, "
    "read_doc and doc_behaviours for related docs this change reaches into, and doc_history or "
    "search_history when you need to know why something is the way it is. Finish by calling "
    "submit_impact, or ask_user when a behaviour is genuinely open.",
)

ACCEPTANCE_INSTRUCTIONS = _join(
    _PANEL_ROLE,
    ACCEPTANCE_RULES,
    'Respond with ONLY a JSON object, no other text:\n{"tests": [{"scenario": "short name", '
    '"given": "…", "when": "…", "then": "…", "covers": "AT-3 or a short behaviour name"}]}',
)

FINAL_INSTRUCTIONS = _join(_PANEL_ROLE, CONVENTION, f"{FINAL_RULES}\n{_PANEL_TABLE_RULE}")

# What a provider with no tool channel is asked to send instead of calling a terminal tool. Parsed
# and then pushed through the *same* terminal tool (`DocToolbox.invoke`), so both paths validate
# identically.
_ENVELOPES = {
    "set_scope": (
        "scope",
        'Respond with ONLY a JSON object, no other text. Either\n{"scope": {"domain": "kebab-case", '
        '"topic": "kebab-case", "type": "feature|workflow", "existing_doc": "specs/… or null", '
        '"reason": "one sentence"}}\nor, to ask the reader instead,\n{"ask": {"question": "…", '
        '"options": [{"label": "…", "domain": "…", "topic": "…", "existing_doc": "…"}]}}',
    ),
    "submit_impact": (
        "impact",
        'Respond with ONLY a JSON object, no other text. Either\n{"impact": {"summary": "…", '
        '"changes": [{"section": "…", "kind": "add|modify|remove", "summary": "…"}], '
        '"behaviours": [{"ref": "AT-3 or empty", "behaviour": "…", "today": "…", "after": "…", '
        '"kind": "changed|new|removed"}]}}\nor, to ask the reader instead,\n{"ask": {"question": '
        '"…", "options": [{"label": "…"}]}}',
    ),
}


def workflow_prompt(request: str) -> str:
    """The whole workflow in words, for an MCP host that runs it with its own model and tools.

    Built from the same rule blocks the panel's prompts are, so the two can't drift.
    """
    return _join(
        f"Draft a spec change for this request, with the reader, in four stages: {request}",
        "You work from this project's docs and git history, through the specky MCP tools "
        "(list_domains, search_docs, read_doc, doc_behaviours, commits_for_doc, search_history). "
        "Do not read source code for this.",
        CONVENTION,
        "## Stage 1 — Scope\n" + SCOPE_RULES + "\nState the target path (new or update) before "
        "moving on.",
        "## Stage 2 — Impact\n" + IMPACT_RULES + "\nShow the reader two tables — Changes "
        "(Section | Change | What) and Behaviours that change (Ref | Behaviour | Today | After) — "
        "and wait for them to confirm or correct it.",
        "## Stage 3 — Acceptance tests\n" + ACCEPTANCE_RULES + "\nRender the rows with the "
        "render_acceptance_table tool, show them, and wait for the reader to APPROVE them. Revise "
        "until they do; do not write the doc before approval.",
        f"## Stage 4 — Final draft\n{FINAL_RULES}\n{_MCP_TABLE_RULE}\nShow the finished doc. "
        "Write it to disk only if the reader asks you to.",
    )


# --- the state that round-trips through the reader's tab ------------------------------------------


@dataclass
class DraftState:
    request: str
    stage: str = STAGE_QUESTION
    # "The reader scoped this to module `chat`", or pointed at a doc we couldn't find.
    hint: str = ""
    scope: dict | None = None
    clarifications: list[dict] = field(default_factory=list)
    question: dict | None = None
    impact: dict | None = None
    tests: list[dict] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def to_payload(self) -> dict:
        return asdict(self)

    @classmethod
    def from_payload(cls, repo_root: Path, raw: Any) -> DraftState:
        """A client-held draft, re-validated field by field.

        Every field goes through the validator that produced it in the first place, so the state a
        reader's tab sends back can't carry anything a stage couldn't have produced. A field that
        fails is an error rather than a silent reset: a reader who clicks Approve on tests that
        quietly vanished would approve nothing.
        """
        if not isinstance(raw, dict):
            raise DraftError("This draft's state is missing — start the draft again.")
        state = cls(request=clip(raw.get("request"), REQUEST_CHARS))
        if not state.request:
            raise DraftError("This draft has no request — start the draft again.")
        state.stage = raw.get("stage") if raw.get("stage") in STAGES else ""
        state.hint = clip(raw.get("hint"), 200)

        scope = raw.get("scope")
        if isinstance(scope, dict):
            rebuilt = scope_payload(
                repo_root,
                scope.get("domain"),
                scope.get("topic"),
                scope.get("type"),
                scope.get("existing"),
                scope.get("reason"),
            )
            if isinstance(rebuilt, str):
                raise DraftError(f"This draft's target no longer checks out: {rebuilt}")
            state.scope = rebuilt

        state.clarifications = _clarifications(raw.get("clarifications"))

        question = raw.get("question")
        if isinstance(question, dict):
            asked = question_payload(question.get("text"), question.get("options"))
            if isinstance(asked, dict):
                resume = question.get("resume")
                state.question = asked | {"resume": resume if resume in RESUMABLE else "scope"}

        impact = raw.get("impact")
        if isinstance(impact, dict):
            found = impact_payload(
                impact.get("summary"), impact.get("changes"), impact.get("behaviours")
            )
            state.impact = found if isinstance(found, dict) else None

        state.tests = tests_payload(raw.get("tests"))
        state.sources = [clip(s, 200) for s in raw.get("sources") or [] if isinstance(s, str)][
            :MAX_SOURCES
        ]

        needs = {
            STAGE_QUESTION: state.question is not None,
            STAGE_IMPACT: state.scope is not None and state.impact is not None,
            STAGE_ACCEPTANCE: state.scope is not None and bool(state.tests),
            STAGE_FINAL: state.scope is not None and bool(state.tests),
        }
        if not needs.get(state.stage, False):
            raise DraftError("This draft's state doesn't hang together — start the draft again.")
        return state


def _clarifications(raw: Any) -> list[dict]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and clip(item.get("a")):
            out.append({"q": clip(item.get("q")), "a": clip(item.get("a"), REPLY_CHARS)})
    return out[-MAX_CLARIFICATIONS:]


def tests_payload(raw: Any) -> list[dict]:
    """Given/When/Then rows as `{scenario, given, when, then, covers}`, rows without a `then`
    dropped — a row that asserts nothing pins nothing down (testgen skips them for the same
    reason)."""
    rows = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not clip(item.get("then")):
            continue
        rows.append(
            {
                "scenario": clip(item.get("scenario"), 120),
                "given": clip(item.get("given")),
                "when": clip(item.get("when")),
                "then": clip(item.get("then")),
                "covers": clip(item.get("covers"), 120),
            }
        )
    return rows[:MAX_TESTS]


def acceptance_table(rows: list[dict]) -> str:
    """The approved rows as the doc's `## Acceptance Tests` table — deterministic, so what lands in
    the draft is exactly what was approved. `| Scenario | Given | When | Then |` is a shape `specky
    tests` already reads (testgen matches columns by header). `covers` stays out: it points at row
    ids that only mean something inside this draft."""

    def cell(text: Any) -> str:
        return clip(text, 1_000).replace("|", r"\|") or "—"

    lines = ["| Scenario | Given | When | Then |", "|---|---|---|---|"]
    for row in tests_payload(rows):
        lines.append(
            f"| {cell(row['scenario'])} | {cell(row['given'])} | {cell(row['when'])} | "
            f"{cell(row['then'])} |"
        )
    return "\n".join(lines)


# --- entry points ---------------------------------------------------------------------------------


def start(repo_root: Path, provider: Provider, request: str, mention: dict | None = None) -> dict:
    """Begin a draft from the reader's request, as far as it can go without them."""
    state = DraftState(request=clip(request, REQUEST_CHARS))
    if not state.request:
        raise DraftError("Describe the change you want drafted.")
    if mention and mention.get("kind") == "module":
        state.hint = f"The reader scoped this to the `{kebab(mention['value'])}` module."
    elif mention and mention.get("kind") == "feature":
        scope = _feature_scope(repo_root, str(mention.get("value", "")))
        if scope:
            state.scope = scope
        else:
            state.hint = f"The reader pointed at `#feature:{clip(mention.get('value'), 80)}`."
    _to_impact(repo_root, provider, state)
    return _respond(state)


def act(
    repo_root: Path,
    provider: Provider,
    action: str,
    raw_state: Any,
    reply: Any = None,
    option: Any = None,
) -> dict:
    """Take one step of a draft the reader is part-way through."""
    if action not in ACTIONS:
        raise DraftError(f"Unknown draft action {action!r}.")
    state = DraftState.from_payload(repo_root, raw_state)
    extras: dict = {}

    if action == "reply":
        text = clip(reply, REPLY_CHARS)
        if not text:
            raise DraftError("Type an answer or a correction first.")
        extras = _reply(repo_root, provider, state, text)
    elif action == "choose":
        extras = _choose(repo_root, provider, state, option)
    elif action == "confirm":
        if state.stage != STAGE_IMPACT:
            raise DraftError("There is no impact to confirm yet.")
        _run_acceptance(repo_root, provider, state)
    elif action == "approve":
        if state.stage != STAGE_ACCEPTANCE:
            raise DraftError("There are no acceptance tests to approve yet.")
        extras = _run_final(repo_root, provider, state)
    else:  # rescope
        state.scope, state.impact, state.tests, state.question = None, None, [], None
        _to_impact(repo_root, provider, state)
    return _respond(state, extras)


def _respond(state: DraftState, extras: dict | None = None) -> dict:
    sources = set(state.sources)
    if state.scope and state.scope.get("existing"):
        sources.add(state.scope["existing"])
    response = {"intent": "spec", "draft": state.to_payload(), "sources": sorted(sources)}
    return response | (extras or {})


def _reply(repo_root: Path, provider: Provider, state: DraftState, text: str) -> dict:
    """Free text from the reader: an answer to the open question, or a correction to whatever the
    current stage produced — which then runs again with it."""
    if state.stage == STAGE_QUESTION:
        asked = state.question or {}
        _remember(state, asked.get("text", ""), text)
        state.question = None
        if asked.get("resume") == "impact" and state.scope:
            _run_impact(repo_root, provider, state)
        else:
            _to_impact(repo_root, provider, state)
        return {}
    if state.stage == STAGE_IMPACT:
        _remember(state, "Correction to the impact", text)
        _run_impact(repo_root, provider, state, previous=state.impact)
        return {}
    if state.stage == STAGE_ACCEPTANCE:
        _remember(state, "Correction to the acceptance tests", text)
        _run_acceptance(repo_root, provider, state, previous=state.tests)
        return {}
    _remember(state, "Correction to the draft", text)
    return _run_final(repo_root, provider, state)


def _choose(repo_root: Path, provider: Provider, state: DraftState, option: Any) -> dict:
    """One of the question's options, clicked. An option that names a place settles the scope
    here, with no model call; any other option is answered as if the reader had typed its label."""
    if state.stage != STAGE_QUESTION or not state.question:
        raise DraftError("There is no open question to answer.")
    options = state.question.get("options") or []
    try:
        picked = options[int(option)]
    except (TypeError, ValueError, IndexError):
        raise DraftError("That option isn't one of the choices offered.") from None
    # Only a question about *where* can be answered with a place. An impact question's options are
    # answers about behaviour, whatever doc a model decorated them with.
    places = state.question.get("resume") == "scope"
    if not places or not (picked.get("domain") or picked.get("existing_doc")):
        return _reply(repo_root, provider, state, picked["label"])

    scope = scope_payload(
        repo_root,
        picked.get("domain"),
        picked.get("topic") or _topic_from(state.request),
        picked.get("type") or (state.scope or {}).get("type") or "feature",
        picked.get("existing_doc"),
        "Chosen by the reader.",
    )
    if isinstance(scope, str):
        return _reply(repo_root, provider, state, picked["label"])
    _remember(state, state.question.get("text", ""), picked["label"])
    state.question, state.scope, state.impact, state.tests = None, scope, None, []
    _run_impact(repo_root, provider, state)
    return {}


def _remember(state: DraftState, question: str, answer: str) -> None:
    state.clarifications = _clarifications(
        state.clarifications + [{"q": question, "a": answer}]
    )


# --- the stages -----------------------------------------------------------------------------------


def _to_impact(repo_root: Path, provider: Provider, state: DraftState) -> None:
    """Settle the scope if it isn't, then work out the impact — unless either stops to ask."""
    if state.scope is None:
        _run_scope(repo_root, provider, state)
        if state.scope is None:
            return
    _run_impact(repo_root, provider, state)


def _run_scope(repo_root: Path, provider: Provider, state: DraftState) -> None:
    toolbox = DocToolbox(repo_root, SCOPE_TOOLS)
    domain_map = domains_text(repo_root)
    if len(domain_map) > DOMAIN_MAP_CHARS:
        domain_map = domain_map[:DOMAIN_MAP_CHARS] + "\n…(map truncated — use search_docs)"
    prefix = f"{SCOPE_INSTRUCTIONS}\n\n--- Every doc that exists ---\n{domain_map}"
    lines = _preamble(state)
    if state.hint:
        lines.insert(1, f"{state.hint} Place it there unless it clearly doesn't fit — then ask.")

    done = _run_tools(provider, toolbox, prefix, "\n\n".join(lines), "set_scope", state.request)
    _note_sources(state, toolbox)
    if done is None:
        # The model stopped without deciding. Ask the reader, with the domains the search ranks
        # for their words — a question they can answer beats an error they can't.
        _ask(state, _fallback_scope_question(repo_root, state.request), resume="scope")
    elif done.kind == "ask":
        _ask(state, done.payload, resume="scope")
    else:
        state.scope = done.payload


def _run_impact(
    repo_root: Path, provider: Provider, state: DraftState, previous: dict | None = None
) -> None:
    toolbox = DocToolbox(repo_root, IMPACT_TOOLS)
    lines = _preamble(state)
    existing = (state.scope or {}).get("existing")
    if existing:
        text = (repo_root / existing).read_text()
        rows = doc_behaviours(repo_root, existing)
        lines += [
            f"--- The target doc now, in full ({existing}) ---\n{text}",
            f"--- Its behaviours, by id ---\n{behaviours_text(rows) or '(none stated)'}",
        ]
    else:
        lines.append(
            "There is no doc at this path yet. Related docs may already state behaviours this "
            "changes — search for them."
        )
    if previous:
        lines.append(
            "Your previous impact, which the reader has corrected (their latest correction is "
            "above) — revise it rather than starting over:\n" + json.dumps(previous, indent=1)
        )

    done = _run_tools(
        provider, toolbox, IMPACT_INSTRUCTIONS, "\n\n".join(lines), "submit_impact", state.request
    )
    _note_sources(state, toolbox)
    if done is None:
        raise DraftError(
            "The model stopped without working out what this changes. Try again, or add detail."
        )
    if done.kind == "ask":
        _ask(state, done.payload, resume="impact")
        return
    state.question, state.impact, state.tests = None, done.payload, []
    state.stage = STAGE_IMPACT


def _run_acceptance(
    repo_root: Path, provider: Provider, state: DraftState, previous: list[dict] | None = None
) -> None:
    lines = _preamble(state, impact=True)
    existing = (state.scope or {}).get("existing")
    if existing:
        rows = [r for r in doc_behaviours(repo_root, existing) if r["id"].startswith("AT-")]
        lines.append(
            f"--- The target doc's acceptance rows now ---\n{behaviours_text(rows) or '(none)'}"
        )
    if previous:
        lines.append(
            "Your previous rows, which the reader has corrected (their latest correction is above) "
            "— revise them rather than starting over:\n" + json.dumps(previous, indent=1)
        )
    raw = provider.generate("\n\n".join(lines), prefix=ACCEPTANCE_INSTRUCTIONS, task="draft")
    data = leading_json_object(strip_code_fence(raw)) or {}
    tests = tests_payload(data.get("tests"))
    if not tests:
        raise DraftError("The model didn't return any acceptance tests. Try again.")
    state.tests = tests
    state.stage = STAGE_ACCEPTANCE


def _run_final(repo_root: Path, provider: Provider, state: DraftState) -> dict:
    """The whole doc, with the approved table spliced in by specky, and what it changes."""
    scope = state.scope or {}
    existing = scope.get("existing")
    meta: dict = {"type": scope.get("type", "feature")}
    before = ""
    lines = _preamble(state, impact=True) + [
        "--- The acceptance tests the reader approved ---\n" + acceptance_table(state.tests),
        doc_style(
            scope.get("type", "feature"),
            domain_title=_title(scope.get("domain", "")),
            topic_title=_title(scope.get("topic", "")),
        ),
    ]
    before_body = ""
    if existing:
        before = (repo_root / existing).read_text()
        meta, before_body = frontmatter.parse(before)
        lines.append(f"--- The doc as it is now ({existing}) — update it ---\n{before_body}")

    raw = provider.generate("\n\n".join(lines), prefix=FINAL_INSTRUCTIONS, task="draft")
    body = frontmatter.parse(strip_code_fence(raw))[1].strip()
    if not body:
        raise DraftError("The model returned an empty draft. Try again.")
    body = merge_sections(body + "\n", {"Acceptance Tests": acceptance_table(state.tests)})
    full = frontmatter.render(meta, body)

    warnings: list[str] = []
    diff = ""
    if existing:
        lost = lost_content(before_body, body)
        if lost:
            warnings.append(f"Compared with {existing}, this draft {lost}. Check before using it.")
        repeated = repeated_sections(before_body, body)
        if repeated:
            warnings.append("The draft repeats " + ", ".join(f"`## {t}`" for t in repeated) + ".")
        diff = "\n".join(
            difflib.unified_diff(
                before.splitlines(),
                full.splitlines(),
                fromfile=existing,
                tofile=existing,
                lineterm="",
            )
        )
        if len(diff) > DIFF_CHARS:
            diff = diff[:DIFF_CHARS] + "\n…(diff truncated)"

    # Imported here for the reason chat_server imports it lazily: answer_render reaches
    # html_render, which imports chat_server, which imports this module.
    from specky.answer_render import render_answer

    state.stage = STAGE_FINAL
    return {
        "answer": full,
        "answer_html": render_answer(repo_root, body),
        "path": scope.get("path", ""),
        "diff": diff,
        "warnings": warnings,
    }


# --- running a tool stage, with or without a tool channel -----------------------------------------


def _run_tools(
    provider: Provider,
    toolbox: DocToolbox,
    prefix: str,
    prompt: str,
    final_tool: str,
    request: str,
) -> StageDone | None:
    """One tool-using stage: the `StageDone` it ended with, or None if it never ended."""
    if supports_tools(provider, "draft"):
        try:
            provider.converse(
                f"{prompt}\n\n{TURN_BUDGET}",
                prefix=prefix,
                tools=toolbox.tools(),
                invoke=toolbox.invoke,
                task="draft",
                max_turns=DRAFT_MAX_TURNS,
                final_tool=final_tool,
            )
        except StageDone as done:
            return done
        except ToolLoopUnsupported:
            pass  # belt and braces, as in document.py: degrade rather than fail a paid-for run
        else:
            return None
    return _degraded(provider, toolbox, prefix, prompt, final_tool, request)


def _degraded(
    provider: Provider,
    toolbox: DocToolbox,
    prefix: str,
    prompt: str,
    final_tool: str,
    request: str,
) -> StageDone | None:
    """One call, no tools, a JSON envelope back — for `provider = "command"`.

    The model can't search, so the search it would have run is run for it and pasted in. What comes
    back goes through the same terminal tool a tool call would have reached, so a degraded stage is
    validated exactly like a real one.
    """
    key, envelope = _ENVELOPES[final_tool]
    related = toolbox.search_docs(request)
    raw = provider.generate(
        f"{prompt}\n\n--- Docs that match the request ---\n{related}\n\n{envelope}",
        prefix=prefix,
        task="draft",
    )
    data = leading_json_object(strip_code_fence(raw))
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("ask"), dict):
        name, arguments = "ask_user", data["ask"]
    elif isinstance(data.get(key), dict):
        name, arguments = final_tool, data[key]
    else:
        return None
    try:
        toolbox.invoke(name, arguments)
    except StageDone as done:
        return done
    # The terminal tool returned its complaint instead of ending the stage. A tool-using model
    # would read it and try again; a single call has no again, so this is a stage that never ended.
    return None


# --- helpers --------------------------------------------------------------------------------------


def _ask(state: DraftState, question: dict, resume: str) -> None:
    state.question = question | {"resume": resume}
    state.stage = STAGE_QUESTION


def _note_sources(state: DraftState, toolbox: DocToolbox) -> None:
    state.sources = list(dict.fromkeys(state.sources + toolbox.read))[:MAX_SOURCES]


def _preamble(state: DraftState, *, impact: bool = False) -> list[str]:
    """What every stage's prompt opens with: the request, the target once there is one, the
    reader's answers so far — and, from the acceptance stage on, the impact they confirmed."""
    lines = [f"The reader's request: {state.request}"]
    if state.scope:
        lines.append(_scope_line(state.scope))
    lines += _clarification_lines(state)
    if impact:
        lines.append(
            "--- The impact the reader confirmed ---\n" + json.dumps(state.impact, indent=1)
        )
    return lines


def _clarification_lines(state: DraftState) -> list[str]:
    if not state.clarifications:
        return []
    pairs = "\n".join(f"- Q: {c['q']}\n  A: {c['a']}" for c in state.clarifications)
    return [f"The reader's answers and corrections so far (most recent last):\n{pairs}"]


def _scope_line(scope: dict) -> str:
    kind = scope.get("type", "feature")
    if scope.get("existing"):
        return f"Target: {scope['existing']} — this UPDATES that existing {kind} doc."
    path, domain = scope.get("path", ""), scope.get("domain", "")
    return f"Target: {path} — a NEW {kind} doc in the `{domain}` domain."


def _title(slug: str) -> str:
    return slug.replace("-", " ").title()


_DRAFTING_WORDS = frozenset(
    """draft write create author outline propose design sketch generate start spec specs doc docs
    documentation feature workflow new add need want must support please""".split()
)


def _topic_from(request: str) -> str:
    """A topic slug for a new doc the reader placed with one click, from their own words."""
    words = [
        w
        for w in re.findall(r"[a-z0-9]+", request.lower())
        if w not in QUESTION_WORDS and w not in _DRAFTING_WORDS
    ]
    return kebab(" ".join(words[:4])) or "new-feature"


def _feature_scope(repo_root: Path, slug: str) -> dict | None:
    """The doc a `#feature:<slug>` mention names, when exactly one doc has that stem."""
    slug = kebab(slug)
    root = paths.docs_root(repo_root)
    if not slug or not root.is_dir():
        return None
    matches = [
        p
        for p in root.glob(f"*/{slug}.md")
        if p.parent.name != paths.HISTORY_DIR_NAME
    ]
    if len(matches) != 1:
        return None
    scope = scope_payload(
        repo_root, "", "", "", matches[0].relative_to(repo_root).as_posix(), "Named by the reader."
    )
    return scope if isinstance(scope, dict) else None


def _fallback_scope_question(repo_root: Path, request: str) -> dict:
    known = list(dict.fromkeys(hit["domain"] for hit in search_docs(repo_root, request, limit=20)))
    if len(known) < 3:
        known += [d["domain"] for d in list_domains(repo_root) if d["domain"] not in known]
    if not known:
        raise DraftError(
            "There are no docs to place this next to yet. Name a module with #module:<name>."
        )
    options = [{"label": domain, "domain": domain} for domain in known[:5]]
    payload = question_payload("Which module does this change belong to?", options)
    assert isinstance(payload, dict)  # a literal question with options always validates
    return payload

