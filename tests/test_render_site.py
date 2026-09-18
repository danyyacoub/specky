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
    # Every request goes through speckyFetch, so `fetch(` itself appears only in that one helper —
    # two calls, the same-origin attempt and the companion-port fallback.
    assert re.findall(r"[^y]fetch\((`[^`]*`)", app_js) == [
        "`${speckyApiBase}${path}`",
        "`${speckyApiBase}${path}`",
    ]
    assert app_js.count("speckyFetch('/chat'") == 1
    assert app_js.count("speckyFetch(`/search") == 1
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
    """Regression: `slug` keyed on domain + stem, so the second of these two silently
    overwrote the first and vanished from the site."""
    write_doc("x/a/same.md", "# A same\n")
    write_doc("x/b/same.md", "# B same\n")
    run_index(tmp_repo)
    site = render_site(tmp_repo).parent
    assert {"x-a-same.html", "x-b-same.html"} <= {p.name for p in site.glob("*.html")}


# --- what the search box can find offline ------------------------------------------------
# `site-data.js` is loaded eagerly by every page, with no gzip and no lazy loading on file://,
# so what these pin is the *budget*: how much doc text ships, and what happens on a repo far
# bigger than this one (a repo with 3,000 commits has ~3,000 specs/history/ docs).


def _search_index(site: Path) -> list[dict]:
    data = (site / "assets" / "site-data.js").read_text()
    return json.loads(re.search(r"const SPECKY_INDEX = (\[.*?\]);\n", data, re.DOTALL).group(1))


def test_a_phrase_in_a_docs_last_paragraph_is_searchable(site, tmp_repo, write_doc):
    """The bug this closes: only a 160-char excerpt shipped, so nothing past the opening two
    sentences was findable offline even though FTS5 had the whole doc."""
    write_doc("billing/long.md", "# Long\n\n" + ("Filler prose. " * 200) + "\nA hyperbolic tangent.\n")
    run_index(tmp_repo)
    entries = _search_index(render_site(tmp_repo).parent)

    long_doc = next(e for e in entries if e["title"] == "Long")
    assert "hyperbolic tangent" in long_doc["body"]
    assert "hyperbolic" not in long_doc["excerpt"]  # display text is still short


def test_the_body_is_lowercased_stripped_plain_text(site):
    """Lowercased at build time so the client isn't lowercasing every doc per keystroke, and
    markdown syntax removed so a search for a word isn't defeated by a `**` next to it."""
    entry = next(e for e in _search_index(site) if e["title"] == "Billing — Refund Flow")
    assert "a refund uses the index." in entry["body"]
    assert entry["body"] == entry["body"].lower()
    assert "|" not in entry["body"] and "#" not in entry["body"]


def test_an_identifier_keeps_its_underscores_in_the_body(tmp_repo, write_doc):
    """Regression: stripping `_` as markdown emphasis turned `busy_timeout` into `busytimeout`, so
    no snake_case identifier — the thing a reader of code docs actually types — matched offline,
    while the served `/search` found it. Silent, and it defeated the whole point of shipping bodies.
    """
    write_doc("db/locking.md", "# Locking\n\nWriters wait via `busy_timeout=5000` instead.\n")
    run_index(tmp_repo)
    entry = next(e for e in _search_index(render_site(tmp_repo).parent) if e["title"] == "Locking")
    assert "busy_timeout" in entry["body"]


def test_each_entry_carries_the_doc_path_the_served_search_returns(site):
    """`GET /search` answers with `specs/...` paths; the client maps them back through this."""
    paths = {e["path"] for e in _search_index(site)}
    assert "specs/billing/refund-flow.md" in paths


@pytest.mark.parametrize(
    "doc_count, regime",
    [
        (10, "whole docs"),  # tiny repo: budget/count is way over the per-doc ceiling
        (500, "adaptive"),  # cap shrinks to fit the total budget
        (3_000, "dropped"),  # past the floor: no bodies ship at all
    ],
)
def test_the_search_payload_stays_bounded_as_a_repo_grows(doc_count, regime):
    """specky is installed into other people's repos. This is the sizing check, so it's
    parametrised over the three regimes rather than over this repo's own doc count."""
    from specky.html_render import (
        SEARCH_BODY_MAX_CHARS,
        SEARCH_BODY_MIN_CHARS,
        SEARCH_BODY_TOTAL_CHARS,
        search_body_cap,
    )

    cap = search_body_cap(doc_count)
    if regime == "dropped":
        assert cap is None
        return
    assert SEARCH_BODY_MIN_CHARS <= cap <= SEARCH_BODY_MAX_CHARS
    assert doc_count * cap <= max(SEARCH_BODY_TOTAL_CHARS, doc_count * SEARCH_BODY_MIN_CHARS)
    if regime == "whole docs":
        assert cap == SEARCH_BODY_MAX_CHARS


def test_a_repo_too_large_for_bodies_ships_none_and_says_so(tmp_repo, write_doc, capsys):
    """3,000 history docs is one 3,000-commit repo, not an extreme. Shipping ~400 chars each
    would be a multi-megabyte blocking script on every page navigation, so the viewer degrades
    to excerpt-only matching and the render says where full-text search lives instead."""
    from specky.html_render import SEARCH_BODY_MIN_CHARS, SEARCH_BODY_TOTAL_CHARS

    doc_count = SEARCH_BODY_TOTAL_CHARS // SEARCH_BODY_MIN_CHARS + 1
    history = tmp_repo / "specs" / "history"
    history.mkdir(parents=True, exist_ok=True)
    for i in range(doc_count):
        (history / f"{i:08x}.md").write_text(f"# Commit {i:08x}\n\nTouched something. " * 20)
    run_index(tmp_repo)
    site = render_site(tmp_repo).parent

    entries = _search_index(site)
    assert len(entries) == doc_count
    assert all("body" not in e for e in entries)
    assert (site / "assets" / "site-data.js").stat().st_size < 1_500_000
    assert "specky serve" in capsys.readouterr().out


def test_the_api_base_starts_same_origin_on_a_served_page(site):
    """`serve --port N` serves the page and the API from that same port, which the baked-in
    SPECKY_CHAT_PORT can't know — so a served page tries its own origin first and only falls back
    to the chat port when a route comes back 404/405/501 (i.e. it's some other web server)."""
    app_js = (site / "assets" / "app.js").read_text()
    assert "let speckyApiBase = SPECKY_SERVED ? '' : SPECKY_API_FALLBACK;" in app_js
    assert "SPECKY_NOT_THE_API = new Set([404, 405, 501])" in app_js


def test_the_chat_widget_carries_a_session_that_outlives_the_page(site):
    """The viewer is many pages and the reader navigates mid-conversation, so the session id and
    the transcript both live in sessionStorage — otherwise every follow-up starts cold."""
    app_js = (site / "assets" / "app.js").read_text()
    assert "const payload = { question, session: chatSession };" in app_js
    assert "specky-chat-session" in app_js and "specky-chat-log" in app_js
    # A file:// page (or a sandboxed frame) can refuse storage outright, and http:// on a real
    # hostname isn't a secure context, so neither call may be made unguarded.
    assert "window.sessionStorage.getItem" in app_js
    assert "crypto?.randomUUID" in app_js


def test_the_chat_panel_can_be_reset(site):
    page = (site / "index.html").read_text()
    app_js = (site / "assets" / "app.js").read_text()
    assert 'id="chat-reset"' in page
    assert "speckyFetch('/chat/reset'" in app_js


def test_the_ask_panel_is_a_column_of_the_page_not_a_popup_over_it(site):
    """It docks as the third flex child of `.body-row`, so opening it reflows the doc column
    instead of covering it — which is the difference between a panel and the old tooltip."""
    page = (site / "billing-refund-flow.html").read_text()
    body_row = page.index('<div class="body-row">')
    assert body_row < page.index('<aside id="ask-panel"') < page.index('id="chat-toggle"')

    css = (site / "assets" / "site.css").read_text()
    assert ".ask-panel" in css
    assert "body.ask-open .ask-panel" in css
    assert ".chat-panel" not in css  # the fixed-position popup rules are gone
    assert "position: fixed" not in css.split(".ask-panel")[1].split("}")[0]


def test_the_panel_can_be_dragged_wider_and_remembers_it(site):
    css = (site / "assets" / "site.css").read_text()
    app_js = (site / "assets" / "app.js").read_text()
    assert "var(--ask-width" in css
    assert ".ask-resize" in css
    assert "setProperty('--ask-width'" in app_js
    assert "specky-ask-width" in app_js
    # A drag that leaves the handle must keep tracking, and the keyboard must be able to do it too.
    assert "setPointerCapture" in app_js
    assert "ArrowLeft" in app_js and "ArrowRight" in app_js


def test_a_narrow_window_gets_the_panel_as_an_overlay(site):
    """Below ~1100px there isn't room for nav + doc + a 440px dock, so the dock stops squeezing
    the doc column and floats over it instead."""
    css = (site / "assets" / "site.css").read_text()
    assert "@media (max-width: 1100px)" in css


def test_the_intent_chips_are_in_the_panel_and_reach_the_request(site):
    page = (site / "index.html").read_text()
    app_js = (site / "assets" / "app.js").read_text()
    for intent in ("auto", "explore", "spec"):
        assert f'data-intent="{intent}"' in page
    # Auto means "server, you decide", so it's an absent field rather than a value to parse.
    assert "if (askIntent !== 'auto') payload.intent = askIntent;" in app_js
    assert "specky-ask-intent" in app_js


def test_an_answer_is_inserted_as_the_html_the_server_sanitized(site):
    app_js = (site / "assets" / "app.js").read_text()
    assert "div.innerHTML = data.answer_html || '';" in app_js
    # One live answer is the only markup that reaches the log. A replayed transcript is the stored
    # markdown and sessionStorage is editable by anything on the origin, so replay uses textContent.
    assert app_js.count("answer_html") == 1
    assert "chat-rich" in (site / "assets" / "site.css").read_text()


def test_a_hover_term_works_on_markup_added_after_the_page_loaded(site):
    """An answer arrives long after DOMContentLoaded, so the glossary tooltip has to be a
    delegated listener rather than one bound per span at load."""
    app_js = (site / "assets" / "app.js").read_text()
    assert "closest?.('[data-term]')" in app_js


def test_a_matched_body_shows_its_surrounding_context_in_the_result_row(site):
    app_js = (site / "assets" / "app.js").read_text()
    assert "SEARCH_CONTEXT_CHARS = 60" in app_js
    assert "hit-context" in app_js
    # Escape first, then promote FTS5's markers — the other order would let doc text inject HTML.
    assert app_js.index("escapeHtml(snippet)") < app_js.index("'<mark>'")


# --- the workflow stepper ---------------------------------------------------------------

_STEPS = (
    "## How It Works\n\n"
    "1. **Request a refund** — the customer asks.\n"
    "2. **Check the cap** — the index says how much is left.\n"
    "3. **Pay it out**\n"
)


@pytest.fixture
def stepped(tmp_repo, write_doc) -> Path:
    """The same steps under both classifications, so the only difference is `doc_type`."""
    write_doc(
        "billing/refund-flow.md",
        f"# Billing — Refund Flow\n\n{_STEPS}",
        {"type": "workflow", "tags": ["refunds"]},
    )
    write_doc(
        "billing/refund-limits.md",
        f"# Billing — Refund Limits\n\n{_STEPS}",
        {"type": "feature", "tags": ["refunds"]},
    )
    run_index(tmp_repo)
    return render_site(tmp_repo).parent


def test_a_workflows_happy_path_renders_as_a_stepper(stepped):
    page = (stepped / "billing-refund-flow.html").read_text()

    assert '<ol class="steps">' in page
    assert '<span class="st">Request a refund</span>' in page
    # The em dash between label and sentence is the separator, not content — the two are now
    # separate elements, so carrying it through would render it hanging at the start of a line.
    assert '<span class="sd">the customer asks.</span>' in page
    # A step with a label and nothing after it gets no empty description span.
    assert '<li><span class="st">Pay it out</span></li>' in page


def test_the_same_steps_in_a_feature_doc_stay_a_plain_list(stepped):
    page = (stepped / "billing-refund-limits.html").read_text()

    assert '<ol class="steps">' not in page
    assert "<li><strong>Request a refund</strong> — the customer asks.</li>" in page


def test_the_stepper_is_styled(stepped):
    assert ".doc ol.steps" in (stepped / "assets" / "site.css").read_text()


def test_a_list_that_is_not_under_how_it_works_is_left_alone(tmp_repo, write_doc):
    write_doc(
        "billing/refund-flow.md",
        "# Billing — Refund Flow\n\n## Outcomes\n\n1. **Paid** — money moved.\n",
        {"type": "workflow", "tags": ["refunds"]},
    )
    run_index(tmp_repo)
    site = render_site(tmp_repo).parent

    assert '<ol class="steps">' not in (site / "billing-refund-flow.html").read_text()


def test_steps_written_without_bold_labels_are_left_alone(tmp_repo, write_doc):
    """A stepper full of empty titles is worse than the plain list it replaced, and a doc
    predating the template is the common case."""
    write_doc(
        "billing/refund-flow.md",
        "# Billing — Refund Flow\n\n## How It Works\n\n1. The customer asks.\n2. We pay.\n",
        {"type": "workflow", "tags": ["refunds"]},
    )
    run_index(tmp_repo)
    site = render_site(tmp_repo).parent

    assert '<ol class="steps">' not in (site / "billing-refund-flow.html").read_text()


def test_a_step_carrying_a_sub_list_leaves_the_whole_section_alone(tmp_repo, write_doc):
    """Regression: `_STEP_ITEM`'s lazy `</li>` stops at a *nested* one, which closed the description
    span inside the sub-list and spilled the leftover `</ul></li>` into the page as stray tags."""
    write_doc(
        "billing/refund-flow.md",
        "# Billing — Refund Flow\n\n## How It Works\n\n"
        "1. **Request a refund** — the customer asks.\n"
        "    - via the portal\n"
        "    - via support\n"
        "2. **Pay it out** — money moves.\n",
        {"type": "workflow", "tags": ["refunds"]},
    )
    run_index(tmp_repo)
    page = (render_site(tmp_repo).parent / "billing-refund-flow.html").read_text()

    assert '<ol class="steps">' not in page
    # The sub-list is intact and nothing leaked out of it.
    assert "<li>via the portal</li>" in page
    assert page.count("<ul>") == page.count("</ul>")
