import json

import pytest

from specky import generator
from specky.commit_doc import Commit
from specky.generator import (
    PENDING_DIR,
    _parse_type_and_tags,
    _strip_code_fence,
    classify_change,
    lost_content,
    merge_sections,
    sync_feature_doc,
    update_modules_index,
)
from specky.testgen import split_sections

from conftest import FakeProvider


def _commit(message: str = "feat: add refunds", diff: str = "+ refund code") -> Commit:
    return Commit(sha="a" * 40, author="Test <t@example.com>", date="2026-01-01", message=message, diff=diff)


# --- response parsing -------------------------------------------------------------------


def test_strip_code_fence():
    assert _strip_code_fence("```json\n{}\n```") == "{}"
    assert _strip_code_fence("```\ntext\n```") == "text"
    assert _strip_code_fence("  bare  ") == "bare"


def test_parse_type_and_tags_defaults_to_feature():
    assert _parse_type_and_tags({}) == ("feature", [])
    assert _parse_type_and_tags({"type": "nonsense"}) == ("feature", [])
    assert _parse_type_and_tags({"type": "workflow", "tags": ["a", 2, "b"]}) == ("workflow", ["a", "b"])


# --- classification ---------------------------------------------------------------------


def test_classify_skip(tmp_repo):
    result = classify_change(tmp_repo, _commit(), FakeProvider('{"skip": true}'))
    assert result.skip and "non-feature-affecting" in result.reason


def test_classify_unparseable_response_skips(tmp_repo):
    result = classify_change(tmp_repo, _commit(), FakeProvider("I think maybe billing?"))
    assert result.skip and "could not parse" in result.reason


def test_classify_missing_domain_skips(tmp_repo):
    result = classify_change(tmp_repo, _commit(), FakeProvider('{"skip": false, "topic": "t"}'))
    assert result.skip and "missing domain/topic" in result.reason


def test_classify_success(tmp_repo):
    provider = FakeProvider(
        '{"skip": false, "domain": "billing", "topic": "refund-flow", '
        '"purpose": "Issue refunds", "type": "workflow", "tags": ["refunds"]}'
    )
    result = classify_change(tmp_repo, _commit(), provider)
    assert (result.domain, result.topic, result.doc_type, result.tags) == (
        "billing",
        "refund-flow",
        "workflow",
        ["refunds"],
    )


def test_classify_prompt_offers_existing_tags_and_docs(tmp_repo, write_doc):
    write_doc("billing/refund-flow.md", "# Refunds\n", {"type": "feature", "tags": ["refunds"]})
    write_doc("history/abc12345.md", "# Commit abc12345\n", {"type": "feature", "tags": ["ignored"]})
    provider = FakeProvider('{"skip": true}')
    classify_change(tmp_repo, _commit(), provider)

    prompt = provider.prompts[0]
    assert "refunds" in prompt
    assert "billing/refund-flow" in prompt  # steer toward updating this doc, not a sibling
    assert "ignored" not in prompt  # specs/history/ is a changelog trail, not a feature doc


# --- the specs/ snapshot both prompts are built from -------------------------------------


def test_existing_docs_reads_purposes_from_modules_and_falls_back_to_the_h1(tmp_repo, write_doc):
    write_doc("billing/refund-flow.md", "# Refunds\n", {"type": "workflow", "tags": ["refunds"]})
    write_doc("search/ranking.md", "# Search — Ranking\n", {"type": "feature", "tags": ["search"]})
    update_modules_index(tmp_repo, "billing", "billing/refund-flow.md", "Issue refunds")

    existing = generator.ExistingDocs.load(tmp_repo)
    assert existing.purposes == {
        "billing/refund-flow": "Issue refunds",  # from the MODULES.md row
        "search/ranking": "Search — Ranking",  # no row, so the doc's own H1
    }
    assert existing.tags_line() == "refunds, search"


def test_existing_docs_tolerates_a_scalar_tags_value(tmp_repo, write_doc):
    """`tags: refunds` (hand-written, no brackets) parses as a string — updating a set with it
    would splice in one character per letter."""
    write_doc("billing/refund-flow.md", "# Refunds\n")
    (tmp_repo / "specs" / "billing" / "refund-flow.md").write_text(
        "---\ntype: feature\ntags: refunds\n---\n\n# Refunds\n"
    )
    assert generator.ExistingDocs.load(tmp_repo).tags == set()


def test_existing_docs_is_walked_once_per_run_not_once_per_doc(tmp_repo, write_doc, monkeypatch):
    """Regression: backfill_tags rebuilt this snapshot inside its loop, re-reading the whole
    tree once per doc."""
    for i in range(4):
        write_doc(f"billing/doc-{i}.md", f"# Doc {i}\n")

    loads = []
    original = generator.ExistingDocs.load
    monkeypatch.setattr(
        generator.ExistingDocs,
        "load",
        classmethod(lambda cls, root: loads.append(root) or original.__func__(cls, root)),
    )
    generator.backfill_tags(tmp_repo, FakeProvider('{"type": "feature", "tags": ["billing"]}'))
    assert len(loads) == 1


def test_backfill_tags_shows_later_docs_the_tags_it_just_assigned(tmp_repo, write_doc):
    write_doc("billing/a.md", "# A\n")
    write_doc("billing/b.md", "# B\n")
    provider = FakeProvider('{"type": "feature", "tags": ["refunds"]}')
    generator.backfill_tags(tmp_repo, provider)

    assert "Existing tags in use: (none yet)" in provider.prompts[0]
    assert "Existing tags in use: refunds" in provider.prompts[1]


def test_sync_feature_doc_records_the_doc_it_wrote(tmp_repo):
    """Two commits in one `specky sync` share a snapshot; the second must be told about the doc
    the first created, or it invents a sibling doc for the same feature."""
    existing = generator.ExistingDocs()
    provider = FakeProvider(
        [
            '{"skip": false, "domain": "billing", "topic": "refund-flow", '
            '"purpose": "Issue refunds", "type": "workflow", "tags": ["refunds"]}',
            "# Billing — Refund Flow\n",
            '{"skip": true}',
        ]
    )
    sync_feature_doc(tmp_repo, _commit(), provider, existing)
    classify_change(tmp_repo, _commit(), provider, existing)

    assert existing.purposes == {"billing/refund-flow": "Issue refunds"}
    assert "billing/refund-flow — Issue refunds" in provider.prompts[2]


# --- MODULES.md index -------------------------------------------------------------------


def test_update_modules_index_creates_the_file_and_section(tmp_repo):
    update_modules_index(tmp_repo, "billing", "billing/refund-flow.md", "Issue refunds")
    text = (tmp_repo / "specs" / "MODULES.md").read_text()
    assert "## Billing" in text
    assert "| [billing/refund-flow.md](billing/refund-flow.md) | Issue refunds |" in text


def test_update_modules_index_is_idempotent(tmp_repo):
    for _ in range(3):
        update_modules_index(tmp_repo, "billing", "billing/refund-flow.md", "Issue refunds")
    text = (tmp_repo / "specs" / "MODULES.md").read_text()
    assert text.count("billing/refund-flow.md](") == 1


def test_update_modules_index_reuses_an_existing_section(tmp_repo):
    modules = tmp_repo / "specs" / "MODULES.md"
    modules.write_text(
        "# Modules\n\n## Billing\n\n| Doc | Purpose |\n|---|---|\n"
        "| [billing/a.md](billing/a.md) | A |\n\n## Search\n\n| Doc | Purpose |\n|---|---|\n"
    )
    update_modules_index(tmp_repo, "billing", "billing/b.md", "B")
    text = modules.read_text()
    assert text.count("## Billing") == 1
    assert text.index("billing/b.md") < text.index("## Search")


def test_a_hand_written_heading_variant_is_reused_not_duplicated(tmp_repo):
    """Found on a real repo: a hand-written `## N-Way Match` gained a `## Nway Match` twin the first
    time specky indexed a doc for that domain, because the heading match was exact string equality.
    Punctuation and spacing don't make it a different domain."""
    modules = tmp_repo / "specs" / "MODULES.md"
    modules.write_text(
        "# Modules\n\n## N-Way Match\n\n| Doc | Purpose |\n|---|---|\n"
        "| [nway-match/document-match.md](nway-match/document-match.md) | Match docs |\n"
    )
    update_modules_index(tmp_repo, "nway-match", "nway-match/price-match.md", "Match prices")

    text = modules.read_text()
    assert "## Nway Match" not in text
    assert text.count("## N-Way Match") == 1
    assert "| [nway-match/price-match.md](nway-match/price-match.md) | Match prices |" in text


def test_a_doc_already_linked_under_another_section_is_not_indexed_twice(tmp_repo):
    """The other half of the same duplicate: the row was already there, under a heading no
    normalization will ever match, so the section lookup missed it and appended a second one. A doc
    linked anywhere in the file is indexed, wherever a human chose to file it."""
    modules = tmp_repo / "specs" / "MODULES.md"
    modules.write_text(
        "# Modules\n\n## Matching (hand-written)\n\n| Doc | Purpose |\n|---|---|\n"
        "| [nway-match/document-match.md](nway-match/document-match.md) | Match docs |\n"
    )
    before = modules.read_text()

    update_modules_index(tmp_repo, "nway-match", "nway-match/document-match.md", "Match docs")
    assert modules.read_text() == before


def test_docs_and_documents_stay_separate_sections(tmp_repo):
    """Normalizing drops punctuation, not letters. Prefix matching would file every `documents/` doc
    under `## Docs`, which are two real domains in this repo's own tree."""
    modules = tmp_repo / "specs" / "MODULES.md"
    modules.write_text(
        "# Modules\n\n## Docs\n\n| Doc | Purpose |\n|---|---|\n| [docs/a.md](docs/a.md) | A |\n"
    )
    update_modules_index(tmp_repo, "documents", "documents/b.md", "B")

    text = modules.read_text()
    assert text.count("## Docs") == 1 and text.count("## Documents") == 1
    assert text.index("docs/a.md") < text.index("## Documents")


def test_update_modules_index_replaces_a_placeholder_row(tmp_repo):
    modules = tmp_repo / "specs" / "MODULES.md"
    modules.write_text(
        "# Modules\n\n## Billing\n\n| Doc | Purpose |\n|---|---|\n| _(none yet)_ | Later. |\n"
    )
    update_modules_index(tmp_repo, "billing", "billing/a.md", "A")
    text = modules.read_text()
    assert "_(none yet)_" not in text
    assert "billing/a.md" in text


# --- end-to-end doc sync ----------------------------------------------------------------


def test_sync_feature_doc_writes_doc_frontmatter_and_index(tmp_repo):
    provider = FakeProvider(
        [
            '{"skip": false, "domain": "billing", "topic": "refund-flow", '
            '"purpose": "Issue refunds", "type": "workflow", "tags": ["refunds"]}',
            "# Billing — Refund Flow\n\n## What It Does\nIssues refunds.\n",
        ]
    )
    result = sync_feature_doc(tmp_repo, _commit(), provider)

    assert result.path == tmp_repo / "specs" / "billing" / "refund-flow.md"
    assert result.written
    text = result.path.read_text()
    assert text.startswith("---\ntype: workflow\ntags: [refunds]\n---\n")
    assert "## What It Does" in text
    assert "billing/refund-flow.md" in (tmp_repo / "specs" / "MODULES.md").read_text()


def test_a_frontmatter_block_the_model_echoed_back_is_not_written_twice(tmp_repo):
    """Regression: a commit that touches a doc under specs/ carries that doc's own frontmatter in
    its diff, so the model copies a `---` block into its answer — and `render()` then prepended a
    second one, leaving a doc whose first block is what every reader and the indexer sees."""
    provider = FakeProvider(
        [
            '{"skip": false, "domain": "billing", "topic": "refund-flow", '
            '"purpose": "p", "type": "workflow", "tags": ["refunds"]}',
            "---\ntype: feature\ntags: [copied, from, the, diff]\n---\n\n# Billing — Refund Flow\n",
        ]
    )
    text = sync_feature_doc(tmp_repo, _commit(), provider).path.read_text()

    assert text.count("---\n") == 2  # one block: its opening and closing fence
    assert text.startswith("---\ntype: workflow\ntags: [refunds]\n---\n")  # classification wins
    assert "copied" not in text


def test_sync_feature_doc_returns_none_when_skipped(tmp_repo):
    assert sync_feature_doc(tmp_repo, _commit(), FakeProvider('{"skip": true}')) is None


def test_sync_feature_doc_preserves_a_hand_authored_related_list(tmp_repo, write_doc):
    write_doc(
        "billing/refund-flow.md",
        "# Old\n",
        {"type": "feature", "tags": ["old"], "related": ["search/fts5-syntax-safety"]},
    )
    provider = FakeProvider(
        [
            '{"skip": false, "domain": "billing", "topic": "refund-flow", '
            '"purpose": "p", "type": "feature", "tags": ["refunds"]}',
            "# New body\n",
        ]
    )
    text = sync_feature_doc(tmp_repo, _commit(), provider).path.read_text()
    assert "related: [search/fts5-syntax-safety]" in text
    assert "tags: [refunds]" in text  # regenerated from this run's classification


# --- refusing a destructive regeneration -------------------------------------------------
#
# Two real regressions are what this section exists for: f087a01 cut specs/cli/check.md from 118
# lines to 63, and eefb94b gutted specs/cli/doctor.md's How It Works section — mermaid flowchart
# included — while keeping every heading and 83% of the file's characters. Both were hand-written
# content, and both went in via the post-commit hook with nobody's approval.

_CLASSIFY = (
    '{"skip": false, "domain": "billing", "topic": "refund-flow", '
    '"purpose": "Issue refunds", "type": "feature", "tags": ["refunds"]}'
)

_WHAT = "Refunds money to a customer who asks for it, once someone approves. " * 8
_HOW = "1. **Check.** The request is examined against the original payment. " * 8
_TESTS = "| Given | When | Then |\n|---|---|---|\n| a paid order | a refund | money back |\n"

# Every prose section here is over MIN_SECTION_CHARS, so the per-section ratio applies to it —
# which is the point: the losses worth catching were thousands of characters, not a stray line.
_BIG_DOC = (
    f"# Billing — Refund Flow\n\n## What It Does\n{_WHAT}\n\n"
    f"## How It Works\n{_HOW}\n\n## Acceptance Tests\n{_TESTS}"
)


def _sections(text: str) -> dict[str, str]:
    return {title: "\n".join(lines) for title, lines in split_sections(text) if title}


def test_a_regeneration_that_drops_a_section_is_refused_and_staged(tmp_repo, write_doc):
    """f087a01's failure mode: three whole `##` sections gone, written without a word."""
    doc = write_doc("billing/refund-flow.md", _BIG_DOC, {"type": "feature", "tags": ["refunds"]})
    before = doc.read_text()
    provider = FakeProvider(
        [_CLASSIFY, f"# Billing — Refund Flow\n\n## What It Does\n{_WHAT}\n\n## Acceptance Tests\n{_TESTS}"]
    )
    result = sync_feature_doc(tmp_repo, _commit(), provider)

    assert doc.read_text() == before  # the doc on disk is untouched, byte for byte
    assert result.path == doc and not result.written
    assert "`## How It Works`" in result.note
    assert (tmp_repo / PENDING_DIR / "billing" / "refund-flow.md").exists()


def test_a_regeneration_that_guts_a_section_it_kept_is_refused(tmp_repo, write_doc):
    """eefb94b's failure mode, and the harder one: every heading survives, so only a per-section
    measurement notices that the content under one of them is gone."""
    doc = write_doc("billing/refund-flow.md", _BIG_DOC, {"type": "feature", "tags": ["refunds"]})
    before = doc.read_text()
    provider = FakeProvider(
        [_CLASSIFY, f"# Billing — Refund Flow\n\n## What It Does\n{_WHAT}\n\n"
         f"## How It Works\n1. **Check.** It gets checked.\n\n## Acceptance Tests\n{_TESTS}"]
    )
    result = sync_feature_doc(tmp_repo, _commit(), provider)

    assert doc.read_text() == before
    assert not result.written
    assert "`## How It Works` keeps" in result.note


def test_a_doc_with_no_sections_is_still_protected_by_the_whole_doc_ratio(tmp_repo, write_doc):
    """The backstop for a rewrite that restructures the headings out from under the section check."""
    doc = write_doc("billing/refund-flow.md", f"# Refunds\n\n{_WHAT}\n", {"type": "feature"})
    provider = FakeProvider([_CLASSIFY, "# Refunds\n\nIt refunds.\n"])
    result = sync_feature_doc(tmp_repo, _commit(), provider)

    assert not result.written
    assert "the doc keeps" in result.note
    assert _WHAT.strip() in doc.read_text()


def test_a_section_update_leaves_every_other_section_byte_identical(tmp_repo, write_doc):
    """The reason the update path asks for sections instead of a file: what the model doesn't
    mention is copied through rather than re-emitted from its reading of it."""
    doc = write_doc("billing/refund-flow.md", _BIG_DOC, {"type": "feature", "tags": ["refunds"]})
    was = _sections(doc.read_text())
    new_how = "1. **Check.** The request is now examined against the refund policy as well. " * 8
    provider = FakeProvider([_CLASSIFY, json.dumps({"sections": {"How It Works": new_how}})])
    result = sync_feature_doc(tmp_repo, _commit(), provider)

    assert result.written
    now = _sections(doc.read_text())
    assert now["What It Does"] == was["What It Does"]
    assert now["Acceptance Tests"] == was["Acceptance Tests"]
    assert "refund policy" in now["How It Works"]


@pytest.mark.parametrize(
    "suffix",
    [
        pytest.param("}", id="one closing brace too many"),
        pytest.param("\n\nThose are the sections I changed.", id="trailing commentary"),
        pytest.param("\n```", id="an unopened closing fence"),
    ],
)
def test_a_section_envelope_with_trailing_junk_is_still_spliced(tmp_repo, write_doc, suffix):
    """DeepSeek closed a 4kB envelope with `}}}` against a real repo. `json.loads` refuses trailing
    data, so the whole splice was read as a replacement body — `lost_content` caught it there, but
    only because the doc had sections to lose: a short doc would have had the envelope's own source
    written into it as content."""
    doc = write_doc("billing/refund-flow.md", _BIG_DOC, {"type": "feature", "tags": ["refunds"]})
    was = _sections(doc.read_text())
    new_how = "1. **Check.** The request is now examined against the refund policy as well. " * 8
    envelope = json.dumps({"sections": {"How It Works": new_how}})
    provider = FakeProvider([_CLASSIFY, envelope + suffix])
    result = sync_feature_doc(tmp_repo, _commit(), provider)

    assert result.written
    now = _sections(doc.read_text())
    assert '"sections"' not in doc.read_text()
    assert now["What It Does"] == was["What It Does"]
    assert now["Acceptance Tests"] == was["Acceptance Tests"]
    assert "refund policy" in now["How It Works"]


def test_a_section_update_wrapped_in_echoed_frontmatter_is_still_spliced(tmp_repo, write_doc):
    """The doc handed to the model starts with a frontmatter block, and `generate_feature_doc`
    already strips the echo of one from a whole-body response; the envelope path strips it too,
    rather than reading the block as the reason the JSON didn't parse."""
    doc = write_doc("billing/refund-flow.md", _BIG_DOC, {"type": "feature", "tags": ["refunds"]})
    new_how = "1. **Check.** The request is now examined against the refund policy as well. " * 8
    envelope = json.dumps({"sections": {"How It Works": new_how}})
    provider = FakeProvider([_CLASSIFY, f"---\ntype: feature\ntags: [refunds]\n---\n\n{envelope}"])
    result = sync_feature_doc(tmp_repo, _commit(), provider)

    assert result.written
    assert '"sections"' not in doc.read_text()
    assert "refund policy" in _sections(doc.read_text())["How It Works"]


def test_a_classification_with_trailing_junk_still_names_its_doc(tmp_repo, write_doc):
    """Same brace off the classification call: the fallback there is a silently skipped commit."""
    write_doc("billing/refund-flow.md", _BIG_DOC, {"type": "feature", "tags": ["refunds"]})
    new_how = "1. **Check.** The request is now examined against the refund policy as well. " * 8
    envelope = json.dumps({"sections": {"How It Works": new_how}})
    result = sync_feature_doc(tmp_repo, _commit(), FakeProvider([_CLASSIFY + "}", envelope]))

    assert result.written
    assert result.path.name == "refund-flow.md"


def test_a_section_update_naming_an_unknown_heading_appends_it(tmp_repo, write_doc):
    write_doc("billing/refund-flow.md", _BIG_DOC, {"type": "feature", "tags": ["refunds"]})
    provider = FakeProvider(
        [_CLASSIFY, json.dumps({"sections": {"Outcomes": "| Result |\n|---|\n| Refunded |\n"}})]
    )
    text = sync_feature_doc(tmp_repo, _commit(), provider).path.read_text()

    assert "## Outcomes" in text
    assert text.index("## Acceptance Tests") < text.index("## Outcomes")  # appended, not spliced


def test_a_doc_marked_authored_human_is_never_regenerated(tmp_repo, write_doc):
    """The explicit opt-out. Checked before generating, so it costs nothing but the classification
    call that identified the doc — and still linked, because a commit touching this feature is
    exactly when its owner should look at it."""
    doc = write_doc(
        "billing/refund-flow.md", _BIG_DOC, {"type": "feature", "tags": ["x"], "authored": "human"}
    )
    before = doc.read_text()
    provider = FakeProvider([_CLASSIFY])  # a second call raises: none is allowed
    result = sync_feature_doc(tmp_repo, _commit(), provider)

    assert doc.read_text() == before
    assert result.path == doc and not result.written
    assert "authored: human" in result.note
    assert len(provider.prompts) == 1


def test_the_authored_human_marker_is_read_loosely(tmp_repo, write_doc):
    """It's hand-written frontmatter, so `Human` and a trailing space have to count. The line that
    stops the hook is no use if the hook is fussy about how it was typed."""
    write_doc("billing/refund-flow.md", "# Old\n", {"type": "feature", "authored": "Human "})
    result = sync_feature_doc(tmp_repo, _commit(), FakeProvider([_CLASSIFY]))
    assert not result.written


def test_a_doc_naming_a_flag_this_cli_does_not_have_is_refused(tmp_repo):
    """The `--single-page` class of error: a flag the model read in a source comment that says the
    flag does *not* exist. argparse knows the real answer."""
    provider = FakeProvider(
        [_CLASSIFY, "# Billing — Refund Flow\n\n## What It Does\nPass `--not-a-real-flag` to skip.\n"]
    )
    result = sync_feature_doc(tmp_repo, _commit(), provider)

    assert not result.written
    assert not result.path.exists()  # a brand-new doc: nothing reached specs/ at all
    assert "`--not-a-real-flag`" in result.note
    assert (tmp_repo / PENDING_DIR / "billing" / "refund-flow.md").exists()


def test_a_doc_naming_a_real_flag_is_written(tmp_repo):
    """The other half of the flag check: it has to let the true ones through."""
    provider = FakeProvider(
        [_CLASSIFY, "# Billing — Refund Flow\n\n## What It Does\nRun `specky doctor --json`.\n"]
    )
    assert sync_feature_doc(tmp_repo, _commit(), provider).written


def test_lost_content_passes_an_honest_update():
    """Across 33 real doc updates in this repo's history no section fell below 98% of its previous
    size, which is the headroom MIN_KEPT_RATIO is set against."""
    tightened = _BIG_DOC.replace("once someone approves. ", "once approved. ")
    assert lost_content(_BIG_DOC, tightened) is None


def test_lost_content_ignores_a_small_section():
    """A short section shrinking is noise, not a rewrite — the losses worth stopping were
    thousands of characters."""
    before = f"# D\n\n## Flags\n`--json` prints JSON.\n\n## What It Does\n{_WHAT}\n"
    after = f"# D\n\n## Flags\nNone.\n\n## What It Does\n{_WHAT}\n"
    assert lost_content(before, after) is None


def test_merge_sections_reproduces_a_doc_it_changes_nothing_in():
    assert merge_sections(_BIG_DOC, {}) == _BIG_DOC.rstrip("\n") + "\n"


def test_merge_sections_matches_a_heading_case_insensitively_and_strips_a_repeated_one():
    merged = merge_sections(_BIG_DOC, {"how it works": "## How It Works\n1. **New.** Different.\n"})
    assert merged.count("## How It Works") == 1
    assert "1. **New.** Different." in merged
    assert _WHAT.strip() in merged


def test_backfill_tags_skips_already_tagged_and_meta_domains(tmp_repo, write_doc):
    write_doc("billing/refund-flow.md", "# Refunds\n")
    write_doc("billing/tagged.md", "# Tagged\n", {"type": "feature", "tags": ["x"]})
    write_doc("history/abc12345.md", "# Commit abc12345\n")
    (tmp_repo / "specs" / "PRODUCT.md").write_text("# Product\n")

    provider = FakeProvider('{"type": "workflow", "tags": ["refunds"]}')
    updated = generator.backfill_tags(tmp_repo, provider)

    assert [p.name for p in updated] == ["refund-flow.md"]
    assert "type: workflow" in (tmp_repo / "specs" / "billing" / "refund-flow.md").read_text()
    assert len(provider.prompts) == 1
