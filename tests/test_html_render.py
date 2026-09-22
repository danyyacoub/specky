import re

import pytest

from specky import html_render
from specky.html_render import (
    _excerpt,
    _nav_title,
    slug,
    _tag_class,
    _wrap_tables,
    anchor_headings,
    link_glossary,
    load_glossary,
    rewrite_links,
)

GLOSSARY = {
    "feature doc": "A reference doc for one bounded capability.",
    "feature": "A bounded capability.",
    "index": "The SQLite database specky builds from specs/ and git log.",
}


# --- slugs ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, expected",
    [
        ("specs/documentation/feature-sync.md", "documentation-feature-sync"),
        ("specs/PRODUCT.md", "root-PRODUCT"),
        ("specs/history/ee5c73c4.md", "history-ee5c73c4"),
        ("specs/a/b/c/deep.md", "a-b-c-deep"),
    ],
)
def testslug(path, expected):
    assert slug(path) == expected


def test_slug_distinguishes_same_stem_in_different_subdirectories():
    """Two docs sharing a domain (first path part) and a stem used to collapse onto one page
    name, and the second render silently overwrote the first."""
    assert slug("specs/x/a/same.md") != slug("specs/x/b/same.md")


def test_root_doc_named_index_cannot_clobber_the_home_page():
    assert slug("specs/index.md") == "root-index"


# --- excerpts and tags ------------------------------------------------------------------


def test_excerpt_drops_the_title_and_markdown_syntax():
    excerpt = _excerpt("# Title\n\nSee **the** [link](http://x) and `code`.\n")
    assert excerpt == "See the link and code."


def test_excerpt_truncates_with_an_ellipsis():
    excerpt = _excerpt("# T\n\n" + "word " * 100, length=20)
    assert len(excerpt) == 21 and excerpt.endswith("…")


def test_tag_class_is_stable_and_in_range():
    assert _tag_class("billing") == _tag_class("billing")
    for tag in ("billing", "refunds", "onboarding", "search", "docs"):
        index = int(_tag_class(tag).removeprefix("tag-"))
        assert 0 <= index < html_render._TAG_COLOR_COUNT


# --- sidebar titles ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, domain, expected",
    [
        ("Billing — Refund Flow", "billing", "Refund Flow"),
        ("Feature Flags — Rollout", "feature-flags", "Rollout"),
        ("Cli - Pr Comment", "cli", "Pr Comment"),
        ("acme — Glossary", "root", "Glossary"),  # a root doc is prefixed with the repo name
        ("Domain Documentation Workflow", "documentation", "Domain Documentation Workflow"),
        ("Search — FTS5 Syntax Safety", "billing", "Search — FTS5 Syntax Safety"),
        ("Pre-commit Hook", "pre", "Pre-commit Hook"),
        ("Commit abc12345", "history", "Commit abc12345"),
    ],
)
def test_nav_title_drops_only_a_prefix_that_is_the_module(title, domain, expected):
    assert _nav_title(title, domain, "acme") == expected


# --- tables -----------------------------------------------------------------------------


def test_wrap_tables_wraps_each_table_in_a_scrollable_figure():
    wrapped = _wrap_tables("<p>a</p><table><tr><td>1</td></tr></table><table></table>")
    assert wrapped.count('<figure class="tw">') == 2
    assert "<p>a</p>" in wrapped


def test_wrap_tables_is_a_noop_without_tables():
    assert _wrap_tables("<p>no tables</p>") == "<p>no tables</p>"


# --- glossary ---------------------------------------------------------------------------


def test_link_glossary_marks_only_the_first_mention():
    out = link_glossary("<p>A feature and another feature.</p>", GLOSSARY)
    assert out.count('class="gl"') == 1


def test_link_glossary_prefers_the_longest_term():
    out = link_glossary("<p>Read the feature doc.</p>", GLOSSARY)
    assert 'data-term="feature doc"' in out


@pytest.mark.parametrize("tag", ["code", "pre", "a", "span", "button"])
def test_link_glossary_skips_marked_up_regions(tag):
    out = link_glossary(f"<{tag}>feature</{tag}>", GLOSSARY)
    assert 'class="gl"' not in out


def test_link_glossary_is_case_insensitive_but_preserves_the_original_casing():
    out = link_glossary("<p>Feature work.</p>", GLOSSARY)
    assert ">Feature<" in out


def test_link_glossary_respects_word_boundaries():
    assert 'class="gl"' not in link_glossary("<p>featureless indexing</p>", GLOSSARY)


def test_link_glossary_without_a_glossary_is_a_noop():
    assert link_glossary("<p>feature</p>", {}) == "<p>feature</p>"


def test_load_glossary_reads_bold_term_rows(tmp_repo):
    (tmp_repo / "specs" / "GLOSSARY.md").write_text(
        "# Glossary\n\n"
        "| Term | Definition |\n|---|---|\n"
        "| **Feature doc** | A doc for one [capability](x.md). |\n"
        "| not bold | ignored |\n"
    )
    assert load_glossary(tmp_repo) == {"Feature doc": "A doc for one capability."}


def test_load_glossary_missing_file(tmp_repo):
    assert load_glossary(tmp_repo) == {}


# --- in-body links and heading anchors ---------------------------------------------------

PAGES = {"specs/cli/check.md": "cli-check.html", "specs/catalog/graph.md": "catalog-graph.html"}


def _to_page(target: str, anchor: str) -> str | None:
    page = PAGES.get(target)
    return page + (f"#{anchor}" if anchor else "") if page else None


@pytest.mark.parametrize(
    "href, expected",
    [
        ("../cli/check.md", "cli-check.html"),
        ("graph.md", "catalog-graph.html"),
        ("./graph.md#edge-cases", "catalog-graph.html#edge-cases"),
        ("../cli/../cli/check.md", "cli-check.html"),
    ],
)
def test_rewrite_links_resolves_a_path_against_the_doc_it_sits_in(href, expected):
    out = rewrite_links(f'<a href="{href}">x</a>', "specs/catalog/feature.md", _to_page)
    assert out == f'<a href="{expected}">x</a>'


def test_rewrite_links_gives_a_bare_fragment_the_docs_own_path():
    seen = []
    rewrite_links('<a href="#edge-cases">x</a>', "specs/cli/check.md", lambda t, a: seen.append((t, a)) or "")
    assert seen == [("specs/cli/check.md", "edge-cases")]


def test_rewrite_links_unwraps_a_link_with_nowhere_to_go():
    out = rewrite_links('<p><a href="../../skills/x/SKILL.md">the <em>skill</em></a></p>', "specs/cli/check.md", _to_page)
    assert out == "<p>the <em>skill</em></p>"


@pytest.mark.parametrize(
    "href", ["https://example.com/a.md", "mailto:a@example.com", "/abs/a.md", "//cdn.example/a.md"]
)
def test_rewrite_links_leaves_absolute_links_alone(href):
    fragment = f'<a href="{href}">x</a>'
    assert rewrite_links(fragment, "specs/cli/check.md", _to_page) == fragment


def test_rewrite_links_keeps_the_links_other_attributes():
    out = rewrite_links('<a href="../cli/check.md" title="Check">x</a>', "specs/catalog/f.md", _to_page)
    assert out == '<a href="cli-check.html" title="Check">x</a>'


@pytest.mark.parametrize(
    "heading, expected",
    [
        ("What Stops A Doc Being Written", "what-stops-a-doc-being-written"),
        ("Scope, And What It Doesn&#x27;t Hide", "scope-and-what-it-doesnt-hide"),
        ("Documentation — Auto Doc Commit", "documentation--auto-doc-commit"),
        ("<code>busy_timeout</code> retries", "busy_timeout-retries"),
    ],
)
def test_anchor_headings_matches_the_anchors_github_gives(heading, expected):
    assert anchor_headings(f"<h2>{heading}</h2>") == f'<h2 id="{expected}">{heading}</h2>'


def test_anchor_headings_ignores_a_glossary_span_inside_the_heading():
    heading = '<h2>The <span class="gl" data-term="index" tabindex="0">index</span></h2>'
    assert anchor_headings(heading).startswith('<h2 id="the-index">')


def test_anchor_headings_numbers_a_repeated_heading():
    out = anchor_headings("<h2>Notes</h2><h3>Notes</h3><h2>Notes</h2>")
    assert re.findall(r'id="([^"]+)"', out) == ["notes", "notes-1", "notes-2"]


def test_anchor_headings_prefixes_every_id():
    assert anchor_headings("<h3>Edge Cases</h3>", prefix="cli-check--") == (
        '<h3 id="cli-check--edge-cases">Edge Cases</h3>'
    )
