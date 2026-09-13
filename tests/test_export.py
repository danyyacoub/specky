"""`specky export` — the docs as one file someone can be handed.

Two things are worth pinning here. First, that the export is the *same* content as the viewer:
it goes through `html_render.render_doc_body`, so a doc's tables, glossary terms and diagrams have
to survive the trip, and the single page has to work with its JavaScript removed rather than in
spite of it. Second, the sizing: `specs/history/` is one doc per commit, so a 3,000-commit repo
would otherwise put 3,000 per-commit notes in front of the docs the reader came for.
"""

from __future__ import annotations

import re

import pytest

from specky import cli
from specky.export import (
    EXPORT_DIR,
    SINGLE_PAGE_NAME,
    confluence_body,
    demote_headings,
    inline_glossary_titles,
    run_export,
)
from specky.indexer import run_index

DOC = (
    "# Billing — Refund Flow\n\n"
    "## What It Does\nA refund uses the index to find the order.\n\n"
    "### Edge cases\nPartial refunds.\n\n"
    "| Step | Who |\n|---|---|\n| Request | Customer |\n\n"
    "```mermaid\ngraph TD;\n  A-->B;\n```\n"
)


@pytest.fixture
def indexed(tmp_repo, write_doc):
    write_doc("billing/refund-flow.md", DOC, {"type": "workflow", "owner": "Payments team"})
    write_doc("billing/refund-limits.md", "# Billing — Refund Limits\n\nCaps refunds.\n")
    write_doc("history/abc12345.md", "# Commit abc12345\n\nTouched refunds.\n")
    (tmp_repo / "specs" / "GLOSSARY.md").write_text(
        "# Glossary\n\n| Term | Definition |\n|---|---|\n| **index** | The SQLite index. |\n"
    )
    run_index(tmp_repo)
    return tmp_repo


def _page(repo) -> str:
    return (repo / EXPORT_DIR / SINGLE_PAGE_NAME).read_text()


# --- the single page --------------------------------------------------------------------


def test_export_without_an_index_is_a_clear_error(tmp_repo):
    with pytest.raises(RuntimeError, match="specky index"):
        run_export(tmp_repo)


def test_every_doc_is_in_the_one_file_with_a_link_to_it(indexed):
    result = run_export(indexed, title="Acme docs")
    page = _page(indexed)

    assert result.doc_count == 3  # the two billing docs plus GLOSSARY.md; history is left out
    for title in ("Billing — Refund Flow", "Billing — Refund Limits"):
        assert title in page
    # Every table-of-contents link resolves to a section on the same page.
    anchors = set(re.findall(r'<section class="doc" id="([^"]+)"', page))
    assert set(re.findall(r'<a href="#([^"]+)"', page)) == anchors
    assert "Acme docs" in page


def test_the_page_carries_no_javascript(indexed):
    """The reason this shape exists: it has to survive being emailed, opened off a share, or fed
    to a PDF printer, none of which is a place to rely on scripts running."""
    run_export(indexed)
    page = _page(indexed)
    assert "<script" not in page
    assert "onclick" not in page and "onload" not in page


def test_the_docs_own_content_is_rendered_not_escaped(indexed):
    run_export(indexed)
    page = _page(indexed)
    assert "<table>" in page and "<th>Step</th>" in page
    assert "<h2" in page  # the doc's `## What It Does`, demoted


def test_a_glossary_term_becomes_a_native_hover(indexed):
    """`link_glossary` emits a span the viewer's script reads. With no script, the definition has
    to travel in an attribute the browser itself understands or it's invisible."""
    run_export(indexed)
    page = _page(indexed)
    assert 'class="gl"' not in page
    assert '<abbr title="The SQLite index.">index</abbr>' in page


def test_owner_and_type_travel_with_each_doc(indexed):
    run_export(indexed)
    page = _page(indexed)
    assert "Who to ask: <strong>Payments team</strong>" in page
    assert "billing/refund-flow.md" in page


def test_the_page_has_exactly_one_h1(indexed):
    """Its own title. Two `h1`s is a broken outline — which is what a screen reader reads and what
    a PDF's bookmark list is built from."""
    run_export(indexed)
    assert len(re.findall(r"<h1[ >]", _page(indexed))) == 1


def test_headings_are_demoted_but_never_past_h6():
    assert demote_headings("<h1>a</h1><h3>b</h3>") == "<h2>a</h2><h4>b</h4>"
    assert demote_headings("<h6>deep</h6>") == "<h6>deep</h6>"


def test_a_glossary_span_with_no_definition_keeps_its_text():
    fragment = '<p>the <span class="gl" data-term="ghost">ghost</span> term</p>'
    assert inline_glossary_titles(fragment, {}) == "<p>the ghost term</p>"


# --- sizing ----------------------------------------------------------------------------
# specky is installed into other people's repos. A repo with 3,000 commits has ~3,000 per-commit
# docs before it has a single feature doc, and this output is one file that someone reads
# start to finish.


def test_per_commit_docs_are_left_out_by_default_and_reported(indexed):
    result = run_export(indexed)
    assert result.history_skipped == 1
    assert "Commit abc12345" not in _page(indexed)
    assert any("--include-history" in note for note in result.notes)


def test_include_history_opts_back_in(indexed):
    result = run_export(indexed, include_history=True)
    assert result.history_skipped == 0
    assert "Commit abc12345" in _page(indexed)


def test_a_history_heavy_repo_exports_only_its_real_docs(tmp_repo, write_doc):
    """The bound is a whole excluded category, not a truncation: nothing a reader asked for is
    dropped, and what grows with the commit count isn't in the file at all."""
    write_doc("billing/refund-flow.md", DOC)
    history = tmp_repo / "specs" / "history"
    history.mkdir(parents=True, exist_ok=True)
    for i in range(1_500):
        (history / f"{i:08x}.md").write_text(f"# Commit {i:08x}\n\nTouched something.\n")
    run_index(tmp_repo)

    result = run_export(tmp_repo)
    assert result.doc_count == 1
    assert result.history_skipped == 1_500
    assert (tmp_repo / EXPORT_DIR / SINGLE_PAGE_NAME).stat().st_size < 50_000


def test_a_big_export_says_it_is_big(indexed, monkeypatch):
    from specky import export

    monkeypatch.setattr(export, "LARGE_EXPORT_BYTES", 10)
    assert any("MB" in note for note in run_export(indexed).notes)


# --- PDF -------------------------------------------------------------------------------


def test_no_weasyprint_still_leaves_the_single_page_and_says_what_to_install(indexed, monkeypatch):
    from specky import export

    monkeypatch.setattr(export.shutil, "which", lambda name: None)
    result = run_export(indexed, pdf=True)

    assert result.pdf is None
    assert (indexed / EXPORT_DIR / SINGLE_PAGE_NAME).exists()
    assert any("weasyprint not found" in note for note in result.notes)


@pytest.fixture
def fake_weasyprint(monkeypatch):
    """Stand in for the `weasyprint` binary, leaving every other subprocess alone.

    `export.subprocess` is the one shared module, so a blanket patch would also intercept the
    mermaid renderer's `node` call and quietly change what the rest of the export did.
    """
    from specky import export

    real_run = export.subprocess.run
    calls: list[list[str]] = []

    def install(returncode: int = 0, stderr: str = "") -> list[list[str]]:
        def fake_run(argv, **kwargs):
            if "weasyprint" not in str(argv[0]):
                return real_run(argv, **kwargs)
            calls.append(list(argv))
            if returncode == 0:
                export.Path(argv[2]).write_bytes(b"%PDF-1.7\n")
            return export.subprocess.CompletedProcess(argv, returncode, "", stderr)

        monkeypatch.setattr(export.shutil, "which", lambda name: "/usr/bin/weasyprint")
        monkeypatch.setattr(export.subprocess, "run", fake_run)
        return calls

    return install


def test_weasyprint_is_handed_the_single_page_and_the_target(indexed, fake_weasyprint):
    calls = fake_weasyprint()
    result = run_export(indexed, pdf=True)

    assert result.pdf is not None and result.pdf.exists()
    assert calls[0][1].endswith(SINGLE_PAGE_NAME) and calls[0][2].endswith(".pdf")


def test_a_failing_weasyprint_is_reported_not_raised(indexed, fake_weasyprint):
    fake_weasyprint(returncode=1, stderr="cairo exploded\n")
    result = run_export(indexed, pdf=True)

    assert result.pdf is None
    assert any("cairo exploded" in note for note in result.notes)


# --- Confluence ------------------------------------------------------------------------


def test_confluence_writes_a_file_per_doc_plus_an_index(indexed):
    result = run_export(indexed, mode="confluence")
    names = {p.name for p in result.written}

    assert "index.xhtml" in names
    assert "billing-refund-flow.xhtml" in names
    assert (indexed / EXPORT_DIR / "index.xhtml").read_text().count("<li>") >= 3


def test_confluence_has_no_html_storage_format_rejects(indexed):
    """Storage format is not HTML: inline `<svg>` is refused outright, and an unknown class is
    dropped, so a scrollable table wrapper or a glossary span is dead weight at best."""
    run_export(indexed, mode="confluence")
    body = (indexed / EXPORT_DIR / "billing-refund-flow.xhtml").read_text()

    assert "<svg" not in body
    assert 'class="gl"' not in body and 'class="tw"' not in body
    assert "<table>" in body  # the table itself stays, just unwrapped


def test_a_fenced_block_becomes_a_code_macro_with_its_source_intact():
    body = confluence_body("# T\n\n```python\nif a < b:\n    pass\n```\n")

    assert 'ac:name="code"' in body
    assert 'ac:name="language">python' in body
    # Inside CDATA the source is verbatim — a `<` that markdown escaped for HTML has to come back,
    # or every copied snippet is subtly wrong.
    assert "if a < b:" in body
    assert "&lt;" not in body.split("CDATA[")[1]


def test_a_mermaid_fence_is_exported_as_its_source(indexed):
    """Confluence has no server-side mermaid and won't take the SVG, so the diagram travels as
    something a reader can at least copy out."""
    run_export(indexed, mode="confluence")
    body = (indexed / EXPORT_DIR / "billing-refund-flow.xhtml").read_text()

    assert "graph TD;" in body
    assert 'ac:name="language">mermaid' not in body  # not a language Confluence highlights


def test_a_cdata_terminator_in_a_doc_cannot_end_the_section_early():
    """A doc that quotes `]]>` — this module's own docs do — would otherwise close the section
    there and leave the rest of the snippet as loose markup."""
    body = confluence_body("# T\n\n```\na ]]> b\n```\n")

    assert "]]]]><![CDATA[>" in body
    # Split across two sections, and the two sections still concatenate back to the source.
    inner = body.split("<![CDATA[", 1)[1].rsplit("]]>", 1)[0]
    assert inner.replace("]]]]><![CDATA[>", "]]>") == "a ]]> b"


# --- the command -----------------------------------------------------------------------


def test_the_command_reports_what_it_wrote(indexed, monkeypatch, capsys):
    monkeypatch.chdir(indexed)
    monkeypatch.setattr("sys.argv", ["specky", "export"])
    cli.main()

    out = capsys.readouterr().out
    assert f"specky export: wrote {SINGLE_PAGE_NAME}" in out
    assert "3 doc(s) exported" in out


def test_asking_for_both_output_shapes_at_once_is_an_error(indexed, monkeypatch):
    """`--pdf` prints the single page, so with `--confluence` one of the two has to be ignored.
    Failing says which; dropping one silently hands over a folder with no PDF in it."""
    monkeypatch.chdir(indexed)
    monkeypatch.setattr("sys.argv", ["specky", "export", "--confluence", "--pdf"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2


def test_an_unknown_mode_is_rejected_before_anything_is_written(indexed):
    with pytest.raises(ValueError, match="unknown export mode"):
        run_export(indexed, mode="pdf")
    assert not (indexed / EXPORT_DIR).exists()
