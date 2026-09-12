"""End-to-end render: a specs/ tree -> index -> a static site on disk.

Assertions here deliberately read the *whole* site directory rather than a single page, so
they keep holding when shared CSS/JS moves out of every page into `assets/`.
"""

import json
import re
from pathlib import Path

import pytest

from specky import chat_server
from specky.html_render import render_site
from specky.indexer import run_index


@pytest.fixture
def site(tmp_repo, write_doc) -> Path:
    write_doc(
        "billing/refund-flow.md",
        "# Billing — Refund Flow\n\nA refund uses the index.\n\n"
        "| Step | Who |\n|---|---|\n| Request | Customer |\n",
        {"type": "workflow", "tags": ["refunds"], "related": ["billing/refund-limits"]},
    )
    write_doc(
        "billing/refund-limits.md",
        "# Billing — Refund Limits\n\nCaps refunds.\n",
        {"type": "feature", "tags": ["refunds", "policy"]},
    )
    write_doc("history/abc12345.md", "# Commit abc12345\n\nTouched refunds.\n")
    (tmp_repo / "specs" / "GLOSSARY.md").write_text(
        "# Glossary\n\n| Term | Definition |\n|---|---|\n| **index** | The SQLite index. |\n"
    )
    run_index(tmp_repo)
    return render_site(tmp_repo).parent


def _all_text(site: Path) -> str:
    return "\n".join(p.read_text() for p in sorted(site.rglob("*")) if p.is_file())


def test_render_without_an_index_is_a_clear_error(tmp_repo):
    with pytest.raises(RuntimeError, match="specky index"):
        render_site(tmp_repo)


def test_a_page_per_doc_plus_a_home_page(site):
    pages = {p.name for p in site.glob("*.html")}
    assert pages == {
        "index.html",
        "billing-refund-flow.html",
        "billing-refund-limits.html",
        "history-abc12345.html",
        "root-GLOSSARY.html",  # GLOSSARY.md is a doc under specs/, so it gets a page too
    }


def test_home_page_reports_the_counts(site):
    home = (site / "index.html").read_text()
    # 4 docs across billing/, history/ and root; only the two billing docs are classified.
    for count, label in [(4, "docs"), (3, "domains"), (1, "features"), (1, "workflows")]:
        assert f'<span class="n">{count}</span><span class="label">{label}</span>' in home


def test_every_doc_reaches_the_client_side_search_index(site):
    index_js = (site / "assets" / "site-data.js").read_text()
    for title in ("Billing — Refund Flow", "Billing — Refund Limits", "Commit abc12345"):
        assert json.dumps(title)[1:-1] in index_js


def test_the_search_index_is_pure_ascii(site):
    """A `<script src>` has no charset of its own, so non-ASCII titles ride in as \\uXXXX
    escapes rather than depending on the page's encoding being applied to the asset."""
    (site / "assets" / "site-data.js").read_text(encoding="ascii")


def test_shared_css_and_js_live_in_assets_not_in_every_page(site):
    assert (site / "assets" / "site.css").read_text().lstrip().startswith(":root")
    assert "SPECKY_INDEX" in (site / "assets" / "site-data.js").read_text()
    assert f"SPECKY_CHAT_PORT = {chat_server.DEFAULT_PORT}" in (
        site / "assets" / "site-data.js"
    ).read_text()

    page = (site / "billing-refund-flow.html").read_text()
    assert '<link rel="stylesheet" href="assets/site.css">' in page
    assert '<script src="assets/site-data.js"></script>' in page
    assert '<script src="assets/app.js"></script>' in page
    assert "--accent:" not in page  # no inlined stylesheet
    assert "SPECKY_INDEX" not in page  # no inlined search index


def test_app_js_is_one_file_so_its_top_level_consts_share_a_scope(site):
    """SEARCH_JS reads `activeTags`, which FILTER_JS declares — separate <script> tags
    would put them in different scopes and break search-under-filter."""
    app_js = (site / "assets" / "app.js").read_text()
    assert "const activeTags" in app_js
    assert "docMatchesFilters" in app_js


def test_every_asset_reference_resolves_to_a_file_that_exists(site):
    refs = set()
    for page in site.glob("*.html"):
        refs |= set(re.findall(r'(?:href|src)="(assets/[^"]+)"', page.read_text()))
    assert refs == {"assets/site.css", "assets/site-data.js", "assets/app.js"}
    assert all((site / ref).is_file() for ref in refs)


def test_reading_the_site_needs_no_server(site):
    """`file://` gives every local file its own opaque origin: relative `<link>`/`<script src>`
    still load, but XHR and ES-module imports don't. Moving the shared assets out of the pages
    only works because of that, so pin it — the one `fetch` left is the chat widget calling the
    companion server the reader opted into starting, not a page loading its own assets."""
    for page in site.glob("*.html"):
        text = page.read_text()
        assert 'type="module"' not in text
        assert "XMLHttpRequest" not in text and "fetch(" not in text

    app_js = (site / "assets" / "app.js").read_text()
    assert not re.search(r"^\s*(?:import|export)\s", app_js, re.MULTILINE)
    assert re.findall(r"fetch\(([^,]*)", app_js) == ["`${SPECKY_API}/chat`"]
    # ...and on file:// that endpoint resolves to the companion server's own port, since a
    # file:// page has no origin for a relative URL to hang off.
    assert "`http://127.0.0.1:${SPECKY_CHAT_PORT}`" in app_js


def test_the_unread_search_index_json_is_gone(site):
    assert not (site / "search_index.json").exists()


def test_a_page_is_small_now_that_it_carries_no_shared_assets(site):
    assert (site / "billing-refund-flow.html").stat().st_size < 15_000


def test_the_active_sidebar_link_is_set_client_side(site):
    """The rail is byte-identical on every page, so no page ships an `active` class."""
    rails = [(site / name).read_text() for name in ("index.html", "billing-refund-flow.html")]
    assert 'class="active"' not in rails[0] + rails[1]
    assert "classList.add('active')" in (site / "assets" / "app.js").read_text()


def test_the_history_group_is_collapsed_by_default(site):
    page = (site / "index.html").read_text()
    assert '<details class="domain-group"><summary><svg class="icon" aria-hidden="true">' in page


def test_related_docs_are_linked_by_page_name(site):
    page = (site / "billing-refund-flow.html").read_text()
    assert 'href="billing-refund-limits.html"' in page


def test_tables_are_wrapped_and_glossary_terms_are_linked(site):
    page = (site / "billing-refund-flow.html").read_text()
    assert '<figure class="tw">' in page
    assert 'data-term="index"' in page


def test_tag_chips_cover_every_tag_in_use(site):
    home = (site / "index.html").read_text()
    assert 'data-tag="refunds"' in home
    assert 'data-tag="policy"' in home


def test_rerender_replaces_a_stale_page(site, write_doc, tmp_repo):
    (tmp_repo / "specs" / "history" / "abc12345.md").unlink()
    run_index(tmp_repo)
    render_site(tmp_repo)
    assert not (site / "history-abc12345.html").exists()


def test_docs_sharing_a_domain_and_a_stem_each_get_their_own_page(tmp_repo, write_doc):
    """Regression: `_slug` keyed on domain + stem, so the second of these two silently
    overwrote the first and vanished from the site."""
    write_doc("x/a/same.md", "# A same\n")
    write_doc("x/b/same.md", "# B same\n")
    run_index(tmp_repo)
    site = render_site(tmp_repo).parent
    assert {"x-a-same.html", "x-b-same.html"} <= {p.name for p in site.glob("*.html")}
