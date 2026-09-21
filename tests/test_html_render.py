import pytest

from specky import html_render
from specky.html_render import (
    _excerpt,
    slug,
    _tag_class,
    _wrap_tables,
    link_glossary,
    load_glossary,
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
