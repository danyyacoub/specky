from specky import generator
from specky.commit_doc import Commit
from specky.generator import (
    _parse_type_and_tags,
    _strip_code_fence,
    classify_change,
    sync_feature_doc,
    update_modules_index,
)

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
    path = sync_feature_doc(tmp_repo, _commit(), provider)

    assert path == tmp_repo / "specs" / "billing" / "refund-flow.md"
    text = path.read_text()
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
    text = sync_feature_doc(tmp_repo, _commit(), provider).read_text()

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
    text = sync_feature_doc(tmp_repo, _commit(), provider).read_text()
    assert "related: [search/fts5-syntax-safety]" in text
    assert "tags: [refunds]" in text  # regenerated from this run's classification


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
