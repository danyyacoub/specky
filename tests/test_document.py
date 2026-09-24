"""`specky document` over a real repo: what the model may reach, and what specky refuses to write.

The model is scripted (`conftest.ToolProvider`) rather than mocked at the HTTP layer, so these run
the real toolbox against a real git repo: the tools resolve real paths, the read set is whatever
was really opened, and the write path applies the same guards production does.

The refusals are the point of this file. A tool loop buys depth by letting a model choose what to
read, and everything it can get wrong it gets wrong invisibly — prose reads the same whether or not
it is true. So most of what follows is about the four things that stop a doc reaching disk.
"""

from __future__ import annotations

from pathlib import Path

from specky import document, frontmatter
from specky.tools import LIST_FILES_MAX, TOOL_RESULT_BUDGET, Toolbox

from conftest import FakeProvider, ToolProvider, git

MARKDOWN = "# Billing — Refund Flow\n\n## What It Does\n\nIssues refunds to customers.\n"


def _src(repo: Path, rel: str, body: str = "def refund_order():\n    return 'refunded'\n") -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _commit(repo: Path, message: str = "add source") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def _submit(**kw) -> tuple[str, dict]:
    payload = {
        "domain": "billing",
        "topic": "refund-flow",
        "type": "feature",
        "tags": ["billing"],
        "purpose": "Issue refunds",
        "sources": ["src/billing/refund.py"],
        "markdown": MARKDOWN,
    }
    payload.update(kw)
    return ("submit_doc", payload)


_READ = ("read_file", {"path": "src/billing/refund.py"})


def _billing_repo(tmp_repo: Path) -> Path:
    _src(tmp_repo, "src/billing/refund.py")
    _commit(tmp_repo)
    return tmp_repo


# --- the happy path -------------------------------------------------------------------------------


def test_it_writes_the_doc_its_index_row_and_its_glossary_terms(tmp_repo):
    _billing_repo(tmp_repo)
    provider = ToolProvider(
        [
            [("search_code", {"query": "refund"})],
            [_READ],
            [
                _submit(
                    glossary_terms=[
                        {"term": "Refund window", "definition": "The period a refund may be asked for."}
                    ]
                )
            ],
        ]
    )

    written = document.document(tmp_repo, "the refund flow", provider, assume_yes=True)

    doc = tmp_repo / "specs" / "billing" / "refund-flow.md"
    assert doc in written
    meta, body = frontmatter.parse(doc.read_text())
    assert meta["type"] == "feature" and meta["tags"] == ["billing"]
    assert meta["sources"] == ["src/billing/refund.py"]
    assert "Issues refunds to customers." in body
    assert "billing/refund-flow.md" in (tmp_repo / "specs" / "MODULES.md").read_text()
    assert "| **Refund window** |" in (tmp_repo / "specs" / "GLOSSARY.md").read_text()


def test_the_model_is_offered_exactly_the_tools_it_should_have(tmp_repo):
    """No write tool, no shell. It produces text; specky decides what reaches disk."""
    _billing_repo(tmp_repo)
    provider = ToolProvider([[_READ], [_submit()]])

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    assert provider.tool_names == [
        "search_code",
        "list_files",
        "outline",
        "read_file",
        "read_doc",
        "history",
        "submit_doc",
    ]


def test_the_call_is_tagged_with_its_task(tmp_repo):
    """Per-task models route on this label, so a mislabelled call silently uses the default."""
    _billing_repo(tmp_repo)
    provider = ToolProvider([[_READ], [_submit()]])

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    assert provider.tasks == ["document"]


# --- the four refusals ----------------------------------------------------------------------------


def test_a_doc_written_without_reading_anything_is_refused(tmp_repo, capsys):
    """The guard against the worst failure available here: a doc written from no source at all is
    fabricated end to end and reads exactly like a real one."""
    _billing_repo(tmp_repo)
    provider = ToolProvider([[("search_code", {"query": "refund"})], [_submit()]])

    assert document.document(tmp_repo, "refunds", provider, assume_yes=True) == []

    assert not (tmp_repo / "specs" / "billing" / "refund-flow.md").exists()
    assert "no source file was read" in capsys.readouterr().out


def test_an_ungrounded_flag_parks_the_draft_instead_of_writing_it(tmp_repo, capsys):
    _billing_repo(tmp_repo)
    body = MARKDOWN + "\nRun it with `--refund-everything`.\n"
    provider = ToolProvider([[_READ], [_submit(markdown=body)]])

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    assert not (tmp_repo / "specs" / "billing" / "refund-flow.md").exists()
    assert (tmp_repo / ".specky" / "pending" / "billing" / "refund-flow.md").exists()
    assert "--refund-everything" in capsys.readouterr().out


def test_an_update_that_guts_a_section_is_refused(tmp_repo, write_doc, capsys):
    """A model shown an existing doc will sometimes summarise it away. `lost_content` is what
    stopped that happening twice in this repo's own history."""
    _billing_repo(tmp_repo)
    write_doc(
        "billing/refund-flow.md",
        "# Billing — Refund Flow\n\n## What It Does\n\n" + "Detail that matters. " * 60 + "\n",
        {"type": "feature", "tags": ["billing"]},
    )
    provider = ToolProvider([[_READ], [_submit(markdown=MARKDOWN)]])

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    kept = (tmp_repo / "specs" / "billing" / "refund-flow.md").read_text()
    assert "Detail that matters." in kept
    assert (tmp_repo / ".specky" / "pending" / "billing" / "refund-flow.md").exists()
    assert "refused" in capsys.readouterr().out


def test_a_human_authored_doc_is_left_alone_and_the_draft_is_kept(tmp_repo, write_doc, capsys):
    """The commit path can check the freeze before generating; here it cannot, because the model
    picks the target — so the draft has already been paid for by the time we know, and throwing it
    away is a courtesy every other refusal extends."""
    _billing_repo(tmp_repo)
    write_doc(
        "billing/refund-flow.md",
        "# Mine\n\n## What It Does\n\nI wrote this.\n",
        {"type": "feature", "tags": ["billing"], "authored": "human"},
    )
    provider = ToolProvider([[_READ], [_submit()]])

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    assert "I wrote this." in (tmp_repo / "specs" / "billing" / "refund-flow.md").read_text()
    parked = tmp_repo / ".specky" / "pending" / "billing" / "refund-flow.md"
    assert "Issues refunds to customers." in parked.read_text()
    out = capsys.readouterr().out
    assert "authored: human" in out and str(parked.relative_to(tmp_repo)) in out


def test_a_parked_draft_does_not_inherit_the_frozen_docs_own_keys(tmp_repo, write_doc):
    _billing_repo(tmp_repo)
    write_doc(
        "billing/refund-flow.md",
        "# Mine\n\n## What It Does\n\nI wrote this.\n",
        {"type": "feature", "tags": ["billing"], "authored": "human", "owner": "#payments"},
    )

    document.document(tmp_repo, "refunds", ToolProvider([[_READ], [_submit()]]), assume_yes=True)

    meta, _ = frontmatter.parse(
        (tmp_repo / ".specky" / "pending" / "billing" / "refund-flow.md").read_text()
    )
    assert "authored" not in meta and "owner" not in meta


def test_read_doc_warns_that_a_frozen_doc_will_not_be_overwritten(tmp_repo, write_doc):
    """Said where it can still change what the model does. Without it the freeze is only found at
    the write, after a whole run has been paid for — which is what happened against a real repo."""
    _billing_repo(tmp_repo)
    write_doc(
        "billing/refund-flow.md",
        "# Mine\n\n## What It Does\n\nI wrote this.\n",
        {"type": "feature", "tags": ["billing"], "authored": "human"},
    )
    toolbox = Toolbox(tmp_repo)

    result = toolbox.invoke("read_doc", {"path": "billing/refund-flow.md"})

    assert "will NOT be overwritten" in result
    assert "I wrote this." in result, "and the doc itself is still returned"


def test_read_doc_says_nothing_extra_about_an_ordinary_doc(tmp_repo, write_doc):
    _billing_repo(tmp_repo)
    write_doc("billing/refund-flow.md", "# Ours\n", {"type": "feature", "tags": ["billing"]})

    result = Toolbox(tmp_repo).invoke("read_doc", {"path": "billing/refund-flow.md"})

    assert "NOT be overwritten" not in result


# --- sources, which are the doc's only claim to coverage ------------------------------------------


def test_sources_are_only_the_files_that_were_actually_read(tmp_repo):
    """A model that lists a file it merely saw in a search result would otherwise claim `specky
    check` coverage of code nothing in the doc is based on."""
    _src(tmp_repo, "src/billing/refund.py")
    _src(tmp_repo, "src/billing/ledger.py")
    _commit(tmp_repo)
    provider = ToolProvider(
        [[_READ], [_submit(sources=["src/billing/refund.py", "src/billing/ledger.py"])]]
    )

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    meta, _ = frontmatter.parse((tmp_repo / "specs" / "billing" / "refund-flow.md").read_text())
    assert meta["sources"] == ["src/billing/refund.py"]


def test_sources_fall_back_to_the_read_set_when_the_model_claims_none(tmp_repo):
    _billing_repo(tmp_repo)
    provider = ToolProvider([[_READ], [_submit(sources=[])]])

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    meta, _ = frontmatter.parse((tmp_repo / "specs" / "billing" / "refund-flow.md").read_text())
    assert meta["sources"] == ["src/billing/refund.py"]


def test_declared_sources_give_the_doc_check_coverage(tmp_repo):
    """`indexer.index_doc_files` derives coverage from git log, which cannot pair a doc with code it
    did not change — so without `sources:` the gate is silently off on exactly these docs."""
    from specky.db import connect
    from specky.indexer import run_index

    _billing_repo(tmp_repo)
    document.document(
        tmp_repo, "refunds", ToolProvider([[_READ], [_submit()]]), assume_yes=True
    )
    _commit(tmp_repo, "docs")
    run_index(tmp_repo)

    conn = connect(tmp_repo)
    try:
        rows = conn.execute(
            "SELECT doc_path FROM doc_files WHERE path = ?", ("src/billing/refund.py",)
        ).fetchall()
    finally:
        conn.close()
    assert ("specs/billing/refund-flow.md",) in rows


# --- what the tools will and won't do -------------------------------------------------------------


def test_read_file_refuses_a_path_outside_the_allowlist(tmp_repo):
    """The whole containment story: a path is reachable only if `source_files` returned it."""
    _billing_repo(tmp_repo)
    (tmp_repo / "untracked.py").write_text("SECRET = 1\n")
    toolbox = Toolbox(tmp_repo)

    assert "not a tracked source file" in toolbox.invoke("read_file", {"path": "untracked.py"})
    assert "not a tracked source file" in toolbox.invoke("read_file", {"path": "../../etc/passwd"})
    assert toolbox.read == []


def test_read_doc_will_not_read_outside_the_docs_tree(tmp_repo):
    _billing_repo(tmp_repo)
    toolbox = Toolbox(tmp_repo)

    assert "only reads the docs tree" in toolbox.invoke(
        "read_doc", {"path": "src/billing/refund.py"}
    )


def test_the_tool_budget_stops_further_reads(tmp_repo):
    """A loop without this re-reads the same large file until the context or the bill runs out."""
    _src(tmp_repo, "src/big.py", "# padding line\n" * 8_000)
    _commit(tmp_repo)
    toolbox = Toolbox(tmp_repo)

    while toolbox.remaining:
        toolbox.invoke("read_file", {"path": "src/big.py"})
    assert toolbox.spent >= TOOL_RESULT_BUDGET
    assert "budget spent" in toolbox.invoke("read_file", {"path": "src/big.py"})


def test_a_bad_argument_comes_back_as_a_result_rather_than_ending_the_run(tmp_repo):
    """The model wrote the arguments, it can see they were wrong, and the next turn usually fixes
    it — an exception here would abandon a run that has already been paid for."""
    _billing_repo(tmp_repo)
    toolbox = Toolbox(tmp_repo)

    assert "search_code" in toolbox.invoke("search_code", {"nonsense": 1})
    assert "No tool named" in toolbox.invoke("nope", {})


def test_scope_hides_other_source_from_every_tool(tmp_repo):
    _src(tmp_repo, "src/billing/refund.py")
    _src(tmp_repo, "src/auth/login.py", "def log_in():\n    pass\n")
    _commit(tmp_repo)
    toolbox = Toolbox(tmp_repo, scope="src/billing")

    assert "login" not in toolbox.invoke("list_files", {})
    assert "not a tracked source file" in toolbox.invoke("read_file", {"path": "src/auth/login.py"})


def test_scope_does_not_hide_the_tests(tmp_repo):
    """Scope says where the subject lives, not what may be consulted to describe it — and a
    feature's tests live in `tests/` by convention, i.e. outside every plausible scope. Excluding
    them costs the doc its best material: a test case is an expected behaviour someone had to make
    pass, which is exactly the Acceptance Tests table being asked for."""
    _src(tmp_repo, "src/billing/refund.py")
    _src(tmp_repo, "tests/test_billing.py", "def test_refund_is_rejected_after_30_days():\n    pass\n")
    _commit(tmp_repo)
    toolbox = Toolbox(tmp_repo, scope="src/billing")

    assert "rejected_after_30_days" in toolbox.invoke("search_code", {"query": "rejected_after"})
    assert "def test_refund" in toolbox.invoke("read_file", {"path": "tests/test_billing.py"})
    # ...but they are still not part of the subject being listed.
    assert "tests/" not in toolbox.invoke("list_files", {})


def test_list_files_describes_the_shape_when_there_is_too_much_to_list(tmp_repo):
    """A bare call used to return every tracked file: thousands of lines answering no question,
    charged against the budget and then resent on every later turn."""
    for i in range(LIST_FILES_MAX + 10):
        _src(tmp_repo, f"src/pkg{i % 7}/mod{i}.py")
    _commit(tmp_repo)
    toolbox = Toolbox(tmp_repo)

    result = toolbox.invoke("list_files", {})

    assert "too many to list" in result
    assert "src/pkg0/  " in result, "the directories are named instead"
    assert "mod3.py" not in result


def test_history_reports_the_commits_touching_a_file(tmp_repo):
    """Code says what a system does; the log is the only place saying what it used to do."""
    _src(tmp_repo, "src/billing/refund.py")
    _commit(tmp_repo, "cap refunds at 30 days")
    toolbox = Toolbox(tmp_repo)

    result = toolbox.invoke("history", {"path": "src/billing/refund.py"})

    assert "cap refunds at 30 days" in result


def test_history_refuses_a_path_outside_the_allowlist(tmp_repo):
    _billing_repo(tmp_repo)
    (tmp_repo / "untracked.py").write_text("x = 1\n")

    assert "not a tracked source file" in Toolbox(tmp_repo).invoke(
        "history", {"path": "untracked.py"}
    )


def test_history_on_a_file_with_no_commits_says_so(tmp_repo):
    _billing_repo(tmp_repo)
    _src(tmp_repo, "src/billing/fresh.py")
    git(tmp_repo, "add", "-A")  # staged, never committed

    assert "No commits touch" in Toolbox(tmp_repo).invoke(
        "history", {"path": "src/billing/fresh.py"}
    )


# --- the conversation as a whole ------------------------------------------------------------------


def test_a_model_that_never_submits_writes_nothing(tmp_repo, capsys):
    _billing_repo(tmp_repo)
    provider = ToolProvider([[_READ], [_READ]], trailing="I could not find it.")

    assert document.document(tmp_repo, "refunds", provider, assume_yes=True) == []

    assert not (tmp_repo / "specs" / "billing").exists()
    assert "without submitting a doc" in capsys.readouterr().out


def test_max_turns_bounds_the_conversation(tmp_repo):
    _billing_repo(tmp_repo)
    provider = ToolProvider([[_READ]] * 20)

    document.document(tmp_repo, "refunds", provider, assume_yes=True, max_turns=3)

    assert provider.turns_taken == 3


def test_dry_run_calls_nothing_at_all(tmp_repo, capsys):
    """Unlike its predecessor's dry run, which still paid for a discovery call."""
    _billing_repo(tmp_repo)
    provider = ToolProvider([[_READ], [_submit()]])

    assert document.document(tmp_repo, "refunds", provider, dry_run=True) == []

    assert provider.prompts == []
    assert not (tmp_repo / "specs" / "billing").exists()
    assert "Document this feature or workflow: refunds" in capsys.readouterr().out


def test_a_repo_with_no_source_calls_nothing(tmp_repo, capsys):
    provider = ToolProvider([[_submit()]])

    assert document.document(tmp_repo, "refunds", provider, assume_yes=True) == []

    assert provider.prompts == []
    assert "no tracked source files" in capsys.readouterr().out


# --- the prompt -----------------------------------------------------------------------------------


def test_the_prefix_lists_docs_and_tags_that_already_exist(tmp_repo, write_doc):
    """The anti-duplicate guard: without it a second run writes `billing-and-invoicing` beside the
    doc that already covers billing."""
    _billing_repo(tmp_repo)
    write_doc("billing/refund-flow.md", "# Billing\n", {"type": "feature", "tags": ["billing"]})
    provider = ToolProvider([[_READ], [_submit(topic="chargebacks")]])

    document.document(tmp_repo, "chargebacks", provider, assume_yes=True)

    assert "billing/refund-flow" in provider.prefixes[0]
    assert "billing" in provider.prefixes[0]


def test_the_prefix_does_not_move_between_runs(tmp_repo):
    """It is resent on every turn of every run, so a byte of per-run content leaking into it turns
    a cached read into a full-price one thirteen times over.

    Both runs here are refused, so the doc set doesn't move underneath them. That the prefix *does*
    change once a doc is written is the point of it — it carries the list of what already exists,
    which is what stops the next run writing a twin."""
    _billing_repo(tmp_repo)
    first = ToolProvider([[("search_code", {"query": "refund"})], [_submit()]])
    document.document(tmp_repo, "refunds", first, assume_yes=True)
    second = ToolProvider([[("search_code", {"query": "x"})], [_submit()]])
    document.document(tmp_repo, "something else entirely", second, assume_yes=True)

    assert first.prefixes[0] == second.prefixes[0]
    assert "refunds" not in first.prefixes[0]


def test_the_request_travels_in_the_volatile_half(tmp_repo):
    _billing_repo(tmp_repo)
    provider = ToolProvider([[_READ], [_submit()]])

    document.document(tmp_repo, "the refund flow", provider, assume_yes=True)

    prefix = provider.prefixes[0]
    assert "the refund flow" in provider.prompts[0].replace(prefix, "")


def test_a_pinned_domain_and_topic_are_stated_in_the_prompt(tmp_repo):
    _billing_repo(tmp_repo)
    provider = ToolProvider([[_READ], [_submit()]])

    document.document(
        tmp_repo, "refunds", provider, domain="billing", topic="refund-flow", assume_yes=True
    )

    assert "`billing`" in provider.prompts[0] and "`refund-flow`" in provider.prompts[0]


# --- the glossary ---------------------------------------------------------------------------------


def test_hand_written_glossary_definitions_win(tmp_repo, write_doc):
    _billing_repo(tmp_repo)
    write_doc(
        "GLOSSARY.md", "# Glossary\n\n| Term | Definition |\n|---|---|\n| **Refund** | Mine. |\n"
    )
    provider = ToolProvider(
        [[_READ], [_submit(glossary_terms=[{"term": "Refund", "definition": "Theirs."}])]]
    )

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    glossary = (tmp_repo / "specs" / "GLOSSARY.md").read_text()
    assert "Mine." in glossary and "Theirs." not in glossary


def test_a_glossary_row_round_trips_through_the_viewers_parser(tmp_repo):
    """GLOSSARY.md has a machine contract on the other side: `html_render.load_glossary` parses it
    back to drive term auto-linking, and degrades silently to a no-op on a row it can't match."""
    from specky.html_render import load_glossary

    _billing_repo(tmp_repo)
    terms = [
        {"term": "Refund window", "definition": "The period a refund may be requested in."},
        {"term": "Chargeback", "definition": "A reversal forced by the card issuer | the bank."},
    ]
    provider = ToolProvider([[_READ], [_submit(glossary_terms=terms)]])

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    parsed = load_glossary(tmp_repo)
    assert "Refund window" in parsed
    assert "Chargeback" in parsed, "a pipe in a definition must not break the row"


# --- updating, and the shape of what lands --------------------------------------------------------


def test_an_existing_doc_is_updated_in_place_rather_than_twinned(tmp_repo, write_doc):
    _billing_repo(tmp_repo)
    write_doc(
        "billing/refund-flow.md",
        "# Billing — Refund Flow\n\n## What It Does\n\nOld text.\n",
        {"type": "feature", "tags": ["billing"], "owner": "#payments"},
    )
    provider = ToolProvider(
        [[_READ], [_submit(markdown=MARKDOWN + "\n## Outcomes\n\nMore than before.\n")]]
    )

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    assert len(list((tmp_repo / "specs" / "billing").glob("*.md"))) == 1
    meta, body = frontmatter.parse((tmp_repo / "specs" / "billing" / "refund-flow.md").read_text())
    assert "Issues refunds to customers." in body
    assert meta["owner"] == "#payments", "a hand-written owner must survive a regeneration"


def test_an_update_names_the_facts_it_dropped(tmp_repo, write_doc, capsys):
    _billing_repo(tmp_repo)
    write_doc(
        "billing/refund-flow.md",
        "# Billing — Refund Flow\n\n## What It Does\n\nCap 250.00 (`refund_cap`).\n",
        {"type": "feature", "tags": ["billing"]},
    )
    provider = ToolProvider([[_READ], [_submit()]])

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    assert "removed 2 fact(s): 250.00, `refund_cap`" in capsys.readouterr().out


def test_a_rerun_keeps_the_type_and_tags_the_doc_already_has(tmp_repo, write_doc):
    """A type is changed by hand, never by one run's guess — the rule the commit hook follows too
    (`generator.settled_type_and_tags`)."""
    _billing_repo(tmp_repo)
    write_doc(
        "billing/refund-flow.md",
        "# Billing — Refund Flow\n\n## What It Does\n\nOld text.\n",
        {"type": "workflow", "tags": ["refunds", "payments"]},
    )
    provider = ToolProvider([[_READ], [_submit(type="feature", tags=["billing"])]])

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    meta, _ = frontmatter.parse((tmp_repo / "specs" / "billing" / "refund-flow.md").read_text())
    assert (meta["type"], meta["tags"]) == ("workflow", ["refunds", "payments"])


def test_frontmatter_the_model_emitted_is_stripped(tmp_repo):
    """A model shown a doc under specs/ copies the block it saw there, and a second one would land
    on top of the real one."""
    _billing_repo(tmp_repo)
    echoed = "---\ntype: workflow\ntags: [wrong]\n---\n\n" + MARKDOWN
    provider = ToolProvider([[_READ], [_submit(markdown=echoed)]])

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    text = (tmp_repo / "specs" / "billing" / "refund-flow.md").read_text()
    meta, _ = frontmatter.parse(text)
    assert meta["type"] == "feature" and meta["tags"] == ["billing"]
    assert text.count("---\n") == 2


def test_a_domain_or_topic_carrying_a_path_is_flattened(tmp_repo):
    """Both become path components, so a `/` or a `..` has to be neutralised before either is
    joined to the docs root."""
    _billing_repo(tmp_repo)
    provider = ToolProvider([[_READ], [_submit(domain="../etc", topic="billing/refunds")]])

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    assert list((tmp_repo / "specs").glob("*/*.md"))
    assert not list(tmp_repo.parent.glob("etc/*.md"))


# --- the provider that cannot hold a conversation --------------------------------------------------


def test_a_provider_with_no_tool_channel_takes_the_degraded_path(tmp_repo, capsys):
    """`provider = "command"` is one stdin and one stdout. The command is often an agent with tools
    of its own — they are just invisible to specky, which is exactly what it has to warn about."""
    _billing_repo(tmp_repo)
    import json

    provider = FakeProvider(
        json.dumps(
            {
                "domain": "billing",
                "topic": "refund-flow",
                "type": "feature",
                "tags": ["billing"],
                "purpose": "Issue refunds",
                "sources": ["src/billing/refund.py"],
                "markdown": MARKDOWN,
            }
        )
    )

    written = document.document(tmp_repo, "refunds", provider, assume_yes=True)

    assert (tmp_repo / "specs" / "billing" / "refund-flow.md") in written
    out = capsys.readouterr().out
    assert "no tool channel" in out and "taken on trust" in out


def test_the_degraded_path_still_refuses_an_unparseable_answer(tmp_repo, capsys):
    _billing_repo(tmp_repo)

    document.document(tmp_repo, "refunds", FakeProvider("I'd rather not."), assume_yes=True)

    assert not (tmp_repo / "specs" / "billing").exists()
    assert "could not read a doc out of the response" in capsys.readouterr().out


def test_the_degraded_path_keeps_a_claim_that_names_real_tracked_source(tmp_repo):
    """There is no read set to check against, so the claim is taken on trust — but a doc must not
    claim coverage of a path that isn't tracked source at all, and dropping `sources:` entirely
    would leave the doc with no `specky check` coverage."""
    import json

    _billing_repo(tmp_repo)
    (tmp_repo / "untracked.py").write_text("x = 1\n")
    provider = FakeProvider(
        json.dumps(
            {
                "domain": "billing",
                "topic": "refund-flow",
                "type": "feature",
                "tags": ["billing"],
                "purpose": "Issue refunds",
                "sources": ["src/billing/refund.py", "untracked.py", "src/invented.py"],
                "markdown": MARKDOWN,
            }
        )
    )

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    meta, _ = frontmatter.parse((tmp_repo / "specs" / "billing" / "refund-flow.md").read_text())
    assert meta["sources"] == ["src/billing/refund.py"]


def test_what_an_unsubmitting_model_said_is_parked_rather_than_lost(tmp_repo, capsys):
    """Those turns were paid for, and what a model says on its way out is usually the doc itself,
    written as prose instead of handed over."""
    _billing_repo(tmp_repo)
    provider = ToolProvider([[_READ]], trailing="# Billing — Refund Flow\n\nThe whole doc, as text.")

    document.document(tmp_repo, "the refund flow", provider, assume_yes=True)

    parked = tmp_repo / ".specky" / "pending" / "unsubmitted" / "the-refund-flow.md"
    assert "The whole doc, as text." in parked.read_text()
    assert str(parked.relative_to(tmp_repo)) in capsys.readouterr().out


def test_the_model_is_told_how_many_turns_it_has(tmp_repo):
    """A budget it cannot see is one it cannot spend well — left to guess, a thorough model reads
    two modules in full and is cut off before it writes anything."""
    _billing_repo(tmp_repo)
    provider = ToolProvider([[_READ], [_submit()]])

    document.document(tmp_repo, "refunds", provider, assume_yes=True, max_turns=9)

    prefix = provider.prefixes[0]
    volatile = provider.prompts[0].replace(prefix, "")
    assert "at most 9 turns" in volatile, "and in the volatile half, or it breaks the cache"


def test_a_diagram_with_a_dangling_class_suffix_is_repaired_on_the_way_in(tmp_repo, capsys):
    """The defect that prompted this check: it renders perfectly and the node is simply unstyled,
    so nothing anywhere reports a problem."""
    _billing_repo(tmp_repo)
    markdown = MARKDOWN + '\n```mermaid\nflowchart LR\n  A["Refund"]:::\n  A --> B\n```\n'
    provider = ToolProvider([[_READ], [_submit(markdown=markdown)]])

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    written = (tmp_repo / "specs" / "billing" / "refund-flow.md").read_text()
    assert ":::" not in written
    assert 'A["Refund"]' in written, "the node survives, only the empty class goes"
    assert "repaired its diagram" in capsys.readouterr().out


def test_an_unparseable_diagram_is_refused_and_parked(tmp_repo, monkeypatch, capsys):
    """The viewer degrades one it can't parse into a fenced block of raw syntax, dropped into the
    middle of a doc written for people who don't read syntax."""
    import specky.mermaid_tool as mermaid_tool

    monkeypatch.setattr(mermaid_tool, "tool_dir", lambda: tmp_repo)
    monkeypatch.setattr("specky.diagram_render.render_mermaid_svg", lambda source: None)

    _billing_repo(tmp_repo)
    markdown = MARKDOWN + "\n```mermaid\nbananachart LR\n  A --> B\n```\n"
    provider = ToolProvider([[_READ], [_submit(markdown=markdown)]])

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    assert not (tmp_repo / "specs" / "billing" / "refund-flow.md").exists()
    assert (tmp_repo / ".specky" / "pending" / "billing" / "refund-flow.md").exists()
    assert "does not parse" in capsys.readouterr().out


def test_a_good_diagram_reaches_disk_untouched(tmp_repo):
    _billing_repo(tmp_repo)
    diagram = (
        "\n```mermaid\nflowchart LR\n  classDef feature fill:#eef1ff\n"
        '  A["Refund"]:::feature\n  A --> B\n```\n'
    )
    provider = ToolProvider([[_READ], [_submit(markdown=MARKDOWN + diagram)]])

    document.document(tmp_repo, "refunds", provider, assume_yes=True)

    assert diagram.strip() in (tmp_repo / "specs" / "billing" / "refund-flow.md").read_text()


def test_a_provider_that_claims_tools_but_refuses_them_still_produces_a_doc(tmp_repo, capsys):
    """The belt-and-braces path, which for a while was a dead end: no tool ran, so the read set was
    empty, and leaving `tool_capable` True sent the write down a `require_reads` refusal that could
    never be satisfied — a safety net that guaranteed the fall."""
    import json

    class Disagreeing(FakeProvider):
        """`supports_tools` says yes because `converse` exists; `converse` then says no."""

        def converse(self, prompt, **kwargs):
            from specky.ai_provider import ToolLoopUnsupported

            raise ToolLoopUnsupported("no tool channel after all")

    _billing_repo(tmp_repo)
    provider = Disagreeing(
        json.dumps(
            {
                "domain": "billing",
                "topic": "refund-flow",
                "type": "feature",
                "tags": ["billing"],
                "purpose": "Issue refunds",
                "sources": ["src/billing/refund.py"],
                "markdown": MARKDOWN,
            }
        )
    )

    written = document.document(tmp_repo, "refunds", provider, assume_yes=True)

    assert (tmp_repo / "specs" / "billing" / "refund-flow.md") in written
    assert "no source file was read" not in capsys.readouterr().out
