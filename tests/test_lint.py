"""`specky lint` — the doc set's own consistency: vocabulary, tags, and numbers two docs disagree on.

Each fixture is a small tree shaped like what an agent-run migration actually shipped: statuses
used across docs that the glossary no longer defined, a tag per doc, and one threshold stated two
ways.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from specky import catalog, cli, lint

GLOSSARY = """# Glossary

| Term | Definition |
|---|---|
| **Tolerance** | Per-organisation price thresholds. |
| **Gap classification** | The verdict on a line: COMPLIANT or DISCREPANCY. |
| **Regular entry** | A goods line. |
"""


def write(repo: Path, rel: str, text: str, tags: list[str] | None = None) -> Path:
    path = repo / "specs" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    head = f"---\ntype: feature\ntags: [{', '.join(tags)}]\n---\n\n" if tags is not None else ""
    path.write_text(head + text)
    return path


@pytest.fixture
def docs(tmp_repo: Path) -> Path:
    (tmp_repo / "specs" / "GLOSSARY.md").write_text(GLOSSARY)
    write(
        tmp_repo,
        "matching/variance.md",
        "# Variance\n\n## Outcomes\n\n| Status | Meaning |\n|---|---|\n| Price variance | billed above |\n"
        "| COMPLIANT | fine |\n\nA **Matched peer** is the other side. Tolerance is €0.01 per line.\n",
        ["matching"],
    )
    write(
        tmp_repo,
        "matching/workflow.md",
        "# Workflow\n\n## Outcomes\n\n| Status | Meaning |\n|---|---|\n| Price variance | reported |\n\n"
        "The matched peer is shown. **Regular entries** only.\n\n"
        "Tolerance is 0.05 per line.\n",
        ["matching", "manual-linking"],
    )
    write(tmp_repo, "billing/refunds.md", "# Refunds\n\nNothing shared.\n", ["billing"])
    return tmp_repo


def findings(repo: Path, only: set[str] | None = None) -> lint.LintReport:
    return lint.run_lint(repo, only=only)


class TestUndefinedTerms:
    def test_a_phrase_used_in_two_docs_without_a_glossary_row_is_reported(self, docs: Path):
        terms = {t.term: t.docs for t in findings(docs).undefined_terms}

        assert terms["Price variance"] == ("specs/matching/variance.md", "specs/matching/workflow.md")
        assert "Matched peer" in terms

    def test_defined_terms_plurals_and_code_values_the_glossary_explains_are_not(self, docs: Path):
        terms = {t.term for t in findings(docs).undefined_terms}

        assert not terms & {"Regular entries", "Tolerance", "COMPLIANT"}

    def test_a_term_one_doc_uses_stays_local(self, docs: Path):
        write(docs, "billing/refunds.md", "# Refunds\n\nThe **Refund window** applies.\n", ["billing"])

        assert "Refund window" not in {t.term for t in findings(docs).undefined_terms}

    def test_a_status_label_counts_only_where_docs_use_it_as_a_name(self, docs: Path):
        """Outcomes tables are full of descriptive labels ("Left alone"). One mentioned in another
        doc's prose isn't shared vocabulary; one another doc also lists as a status is."""
        write(docs, "billing/refunds.md", "# Refunds\n\n## Outcomes\n\n| Status | Meaning |\n|---|---|\n| Left alone | x |\n", ["billing"])
        write(docs, "billing/other.md", "# Other\n\nThe file is left alone.\n", ["billing"])

        assert "Left alone" not in {t.term for t in findings(docs).undefined_terms}

    def test_section_headings_and_code_labels_are_not_terms(self, docs: Path):
        for rel in ("billing/a.md", "billing/b.md"):
            write(
                docs,
                rel,
                "# A\n\n**Edge cases** below.\n\n## Edge Cases\n\nNone.\n\n## Outcomes\n\n"
                "| Command | Meaning |\n|---|---|\n| `specky tags` | lists |\n",
                ["billing"],
            )

        assert not {"Edge cases", "specky tags"} & {t.term for t in findings(docs).undefined_terms}

    def test_a_single_word_counts_only_where_docs_use_it_as_a_name(self, docs: Path):
        write(docs, "billing/refunds.md", "# Refunds\n\n## Outcomes\n\n| Status | Meaning |\n|---|---|\n| Source | x |\n", ["billing"])
        write(docs, "billing/other.md", "# Other\n\nThe source of truth, from any source.\n", ["billing"])

        assert "Source" not in {t.term for t in findings(docs).undefined_terms}


class TestTags:
    def test_without_a_registry_a_tag_on_one_doc_is_sprawl(self, docs: Path):
        report = findings(docs)

        assert not report.registry
        assert {(t.tag, t.problem) for t in report.tag_problems} == {
            ("manual-linking", "single"),
            ("billing", "single"),
        }

    def test_with_a_registry_only_unregistered_tags_are_flagged(self, docs: Path):
        (docs / "specs" / "TAGS.md").write_text(
            "# Tags\n\n| Tag | Meaning |\n|---|---|\n| **matching** | Pairing documents. |\n| **billing** | Money out. |\n"
        )
        report = findings(docs)

        assert report.registry
        assert [(t.tag, t.problem) for t in report.tag_problems] == [("manual-linking", "unregistered")]

    def test_write_seeds_the_registry_additively(self, docs: Path):
        path, added = catalog.write_tag_registry(docs)

        assert added == ["billing", "manual-linking", "matching"]
        assert catalog.load_tag_registry(docs)["matching"] == "Used by matching/variance, matching/workflow"

        path.write_text(path.read_text().replace("Used by matching/variance, matching/workflow", "Pairing."))
        write(docs, "billing/invoices.md", "# Invoices\n", ["invoicing"])
        _, again = catalog.write_tag_registry(docs)

        assert again == ["invoicing"]
        assert catalog.load_tag_registry(docs)["matching"] == "Pairing.", "a curated meaning is kept"

    def test_the_registry_is_what_the_classifier_is_offered(self, docs: Path):
        from specky.generator import ExistingDocs

        (docs / "specs" / "TAGS.md").write_text("| Tag | Meaning |\n|---|---|\n| **matching** | x |\n")

        line = ExistingDocs.load(docs).tags_line()
        assert line.startswith("matching (this repo's TAGS.md registry")
        assert "manual-linking" not in line


class TestConflicts:
    def test_disjoint_numbers_for_one_name_in_docs_sharing_a_tag(self, docs: Path):
        (conflict,) = findings(docs).conflicts

        assert conflict.key == "tolerance"
        assert {(c.doc, c.value) for c in conflict.claims} == {
            ("specs/matching/variance.md", "0.01"),
            ("specs/matching/workflow.md", "0.05"),
        }

    def test_a_value_further_along_the_other_docs_line_is_agreement(self, docs: Path):
        """"… or 50 (DPGF)" against "DPGF over 30 PDF pages, or more than 50 pages in total": the 50 is
        too far from the word to be claimed for it, but it's plainly the same limit."""
        write(docs, "matching/variance.md", "# V\n\nTolerance allows 20, or 50 in total.\n", ["matching"])
        write(
            docs,
            "matching/workflow.md",
            "# W\n\nTolerance over 30 lines, or more than a grand total of 50 lines overall.\n",
            ["matching"],
        )

        assert findings(docs).conflicts == []

    def test_flags_and_plain_words_in_backticks_are_not_quantities(self, docs: Path):
        write(docs, "matching/variance.md", "# V\n\nWithout `--yes` it stops at 25 commits.\n", ["matching"])
        write(docs, "matching/workflow.md", "# W\n\nWithout `--yes` it stops at 20 files.\n", ["matching"])

        assert findings(docs).conflicts == []

    def test_one_value_in_common_is_agreement(self, docs: Path):
        write(
            docs,
            "matching/workflow.md",
            "# Workflow\n\nTolerance is 0.05, or €0.01 absolute.\n",
            ["matching"],
        )

        assert findings(docs).conflicts == []

    def test_docs_sharing_no_tag_are_not_compared(self, docs: Path):
        write(docs, "billing/refunds.md", "# Refunds\n\nOur tolerance is 12.5 here.\n", ["billing"])

        assert all("billing/refunds.md" not in {c.doc for c in x.claims} for x in findings(docs).conflicts)

    def test_example_sections_are_not_claims(self, docs: Path):
        write(
            docs,
            "matching/workflow.md",
            "# Workflow\n\n## Acceptance Tests\n\n| Given | Then |\n|---|---|\n| Tolerance at 0.07 | flagged |\n",
            ["matching"],
        )

        assert findings(docs).conflicts == []


def test_scope_limits_findings_to_the_docs_named(docs: Path):
    report = findings(docs, only={"specs/billing/refunds.md"})

    assert report.docs == 1
    assert [t.tag for t in report.tag_problems] == ["billing"]
    assert report.undefined_terms == [] and report.conflicts == []


class TestCli:
    def run(self, monkeypatch, *argv: str) -> None:
        monkeypatch.setattr(sys, "argv", ["specky", "lint", *argv])
        cli.main()

    def test_advice_exits_0_and_names_every_kind(self, docs: Path, monkeypatch, capsys):
        monkeypatch.chdir(docs)
        self.run(monkeypatch)

        out = capsys.readouterr().out
        assert out.startswith("specky lint: 3 doc(s) checked")
        assert "Price variance — 2 docs" in out
        assert "manual-linking — specs/matching/workflow.md" in out
        assert "specs/matching/variance.md: 0.01" in out

    def test_strict_exits_1_on_a_finding(self, docs: Path, monkeypatch):
        monkeypatch.chdir(docs)
        with pytest.raises(SystemExit) as exit_info:
            self.run(monkeypatch, "--strict")
        assert exit_info.value.code == 1

    def test_paths_scope_the_report_and_json_carries_it(self, docs: Path, monkeypatch, capsys):
        monkeypatch.chdir(docs / "specs")
        self.run(monkeypatch, "billing", "--json")

        data = json.loads(capsys.readouterr().out)
        assert data["docs"] == 1
        assert [t["tag"] for t in data["tag_problems"]] == ["billing"]

    def test_a_clean_tree_says_so(self, tmp_repo: Path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_repo)
        self.run(monkeypatch)

        assert "Nothing to report." in capsys.readouterr().out
