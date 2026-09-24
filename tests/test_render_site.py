"""End-to-end render: a specs/ tree -> index -> a static site on disk.

Assertions here deliberately read the *whole* site directory rather than a single page, so
they keep holding when shared CSS/JS moves out of every page into `assets/`.
"""

import json
import re
from pathlib import Path

import pytest

from specky import chat_server
from specky.diagram_render import GLASS_DEFS
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


def test_every_page_carries_the_diagram_glass_defs_once(site):
    """The Spec Assistant can put a diagram on any page, so every page needs the gradient and shadow
    its glass references — once, since a second copy would be a duplicate id."""
    for page in site.glob("*.html"):
        assert page.read_text().count(GLASS_DEFS) == 1, page.name


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


def test_sidebar_titles_drop_the_module_the_group_already_names(site):
    page = (site / "index.html").read_text()
    assert "<span>Refund Flow</span>" in page
    assert 'title="Billing — Refund Flow"' in page
    assert "<span>Billing — Refund Flow</span>" not in page


def test_sidebar_links_carry_their_type_icon(site):
    page = (site / "index.html").read_text()
    types = re.findall(r'<a href="[^"]+" title="[^"]*"[^>]*data-type="(\w*)"[^>]*>.*?</a>', page)
    assert set(types) == {"feature", "workflow", ""}
    assert re.search(r'data-type="workflow"[^>]*><svg[^>]*><use href="#icon-cycle">', page)
    assert re.search(r'data-type="feature"[^>]*><svg[^>]*><use href="#icon-sparkle">', page)
    assert re.search(r'data-type=""[^>]*><svg[^>]*><use href="#icon-file-text">', page)


def test_a_doc_page_is_tinted_by_its_type(site):
    assert '<div class="doc" data-type="workflow">' in (site / "billing-refund-flow.html").read_text()
    assert '<div class="doc">' in (site / "index.html").read_text()


def test_related_docs_are_linked_by_page_name(site):
    page = (site / "billing-refund-flow.html").read_text()
    assert 'href="billing-refund-limits.html"' in page


def _related_block(page: str) -> str:
    match = re.search(r'<div class="related">.*?</div>', page, re.S)
    return match.group(0) if match else ""


def test_docs_sharing_a_tag_are_related_without_anyone_writing_the_link(site):
    """refund-limits names no `related:`, but shares `refunds` with refund-flow — so its page lists
    refund-flow, and says which tag they share."""
    block = _related_block((site / "billing-refund-limits.html").read_text())

    assert 'href="billing-refund-flow.html"' in block
    assert '<span class="why">refunds</span>' in block


def test_a_hand_written_related_link_is_not_listed_twice(site):
    block = _related_block((site / "billing-refund-flow.html").read_text())

    assert block.count('href="billing-refund-limits.html"') == 1
    assert '<span class="why">' not in block


def test_history_docs_and_untagged_docs_are_never_tag_siblings(site):
    block = _related_block((site / "billing-refund-limits.html").read_text())

    assert "history-abc12345.html" not in block and "root-GLOSSARY.html" not in block


# --- links inside a doc's body -------------------------------------------------------------
# Docs link each other the way they read in the repo (`../cli/check.md`). The site has no .md
# files and no folders, so each such link has to land on the page its target was rendered to.

LINKING_DOC = (
    "# Billing — Refund Links\n\n"
    "See [the limits](refund-limits.md), [the steps](../billing/refund-flow.md#how-it-works), "
    "[below](#edge-cases), [the skill](../../skills/x/SKILL.md), [a gone doc](gone.md) "
    "and [elsewhere](https://example.com/a.md).\n\n"
    "## Edge Cases\n\nNone.\n"
)


@pytest.fixture
def linked_site(tmp_repo, write_doc) -> Path:
    write_doc("billing/refund-flow.md", "# Billing — Refund Flow\n\n## How It Works\n\nSteps.\n")
    write_doc("billing/refund-limits.md", "# Billing — Refund Limits\n\nCaps refunds.\n")
    write_doc("billing/refund-links.md", LINKING_DOC)
    run_index(tmp_repo)
    return render_site(tmp_repo).parent


@pytest.fixture
def linked(linked_site) -> str:
    return (linked_site / "billing-refund-links.html").read_text()


def test_a_link_to_another_doc_opens_its_page(linked):
    assert '<a href="billing-refund-limits.html">the limits</a>' in linked


def test_a_link_into_another_docs_section_keeps_its_fragment(linked):
    assert '<a href="billing-refund-flow.html#how-it-works">the steps</a>' in linked


def test_a_section_link_lands_on_a_heading_with_that_id(linked):
    assert '<a href="#edge-cases">below</a>' in linked
    assert '<h2 id="edge-cases">' in linked


def test_the_target_page_has_the_heading_a_link_into_it_names(linked_site):
    assert '<h2 id="how-it-works">' in (linked_site / "billing-refund-flow.html").read_text()


def test_a_link_to_something_the_site_did_not_render_keeps_only_its_words(linked):
    assert "the skill" in linked and "SKILL.md" not in linked
    assert "a gone doc" in linked and "gone.md" not in linked


def test_an_absolute_link_is_left_alone(linked):
    assert '<a href="https://example.com/a.md">elsewhere</a>' in linked


def test_no_page_links_a_markdown_file(linked_site):
    assert not re.search(r'href="(?![a-z]+:)[^"]*\.md[#"]', _all_text(linked_site))


def test_a_workflow_stepper_survives_its_heading_getting_an_id(tmp_repo, write_doc):
    """Anchors are added after `render_doc_body`, because `_step_list` matches a bare `<h2>`."""
    write_doc(
        "billing/steps.md",
        "# Billing — Steps\n\n## How It Works\n\n1. **Ask** — the customer asks.\n"
        "2. **Pay** — the refund is paid.\n",
        {"type": "workflow"},
    )
    run_index(tmp_repo)
    page = (render_site(tmp_repo).parent / "billing-steps.html").read_text()
    assert '<h2 id="how-it-works">How It Works</h2>' in page
    assert '<ol class="steps">' in page


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


def test_the_assistant_panel_is_a_column_of_the_page_not_a_popup_over_it(site):
    """It docks as the third flex child of `.body-row`, so opening it reflows the doc column
    instead of covering it — which is the difference between a panel and the old tooltip."""
    page = (site / "billing-refund-flow.html").read_text()
    body_row = page.index('<div class="body-row">')
    assert body_row < page.index('<aside id="assistant-panel"') < page.index('id="chat-toggle"')

    css = (site / "assets" / "site.css").read_text()
    assert ".assistant-panel" in css
    assert "body.assistant-open .assistant-panel" in css
    assert ".chat-panel" not in css  # the fixed-position popup rules are gone
    assert "position: fixed" not in css.split(".assistant-panel")[1].split("}")[0]


def test_the_panel_is_called_the_spec_assistant(site):
    page = (site / "index.html").read_text()
    assert '</svg>Spec Assistant<kbd class="chat-kbd"' in page
    assert 'aria-label="Spec Assistant"' in page
    assert "</svg>Ask</button>" not in page
    assert "Ask about these docs" not in page


def test_the_panel_can_be_dragged_wider_and_remembers_it(site):
    css = (site / "assets" / "site.css").read_text()
    app_js = (site / "assets" / "app.js").read_text()
    assert "var(--assistant-width" in css
    assert ".assistant-resize" in css
    assert "setProperty('--assistant-width'" in app_js
    assert "specky-assistant-width" in app_js
    # A drag that leaves the handle must keep tracking, and the keyboard must be able to do it too.
    assert "setPointerCapture" in app_js
    assert "ArrowLeft" in app_js and "ArrowRight" in app_js
    # The drag cap depends on this window and the rail; restoring the saved width on the next page
    # must not re-cap and re-save it, or one narrow page would shrink it for the rest of the tab.
    assert "applyAssistantWidth(storedAssistantWidth, ASSISTANT_WIDTH_MAX);" in app_js
    # The doc column keeps its room beside the rail, and the overlay isn't held to that.
    assert "max-width: calc(100vw - 360px - var(--rail-width));" in css
    assert "const overlaid = getComputedStyle(assistantPanel).position === 'fixed';" in app_js


def test_a_narrow_window_gets_the_panel_as_an_overlay(site):
    """Below ~1100px there isn't room for nav + doc + a 560px dock, so the dock stops squeezing
    the doc column and floats over it instead."""
    css = (site / "assets" / "site.css").read_text()
    assert "@media (max-width: 1100px)" in css


def test_the_intent_chips_are_in_the_panel_and_reach_the_request(site):
    page = (site / "index.html").read_text()
    app_js = (site / "assets" / "app.js").read_text()
    for intent in ("auto", "explore", "spec"):
        assert f'data-intent="{intent}"' in page
    # Auto means "server, you decide", so it's an absent field rather than a value to parse.
    assert "if (assistantIntent !== 'auto') payload.intent = assistantIntent;" in app_js
    assert "specky-assistant-intent" in app_js


def test_the_intent_chips_sit_in_the_composer_next_to_the_input(site):
    """The answer style is picked where the question is typed, not at the top of the panel."""
    page = (site / "index.html").read_text()
    order = ['id="chat-form"', 'id="chat-input"', 'data-intent="auto"', 'class="chat-send"', "</form>"]
    assert [page.index(marker) for marker in order] == sorted(page.index(marker) for marker in order)
    # The chips are buttons in the form now, so Send's accent styling must not reach them.
    assert ".chat-form button {" not in (site / "assets" / "site.css").read_text()


def test_the_thinking_line_sits_right_above_the_input(site):
    page = (site / "index.html").read_text()
    app_js = (site / "assets" / "app.js").read_text()
    css = (site / "assets" / "site.css").read_text()
    # Below the log, so no longer in the header above it — and floated over the log's bottom edge
    # by the stage they share, rather than taking a row of its own.
    order = [
        'class="chat-stage"', 'id="chat-log"', 'id="chat-thinking"', 'id="chat-status"',
        'id="chat-elapsed"', 'id="chat-stop"', 'id="chat-form"',
    ]
    assert [page.index(marker) for marker in order] == sorted(page.index(marker) for marker in order)
    assert "chatThinking.hidden = !text;" in app_js
    # Every status goes through setChatStatus, or the line would show text while still hidden.
    assert app_js.count("chatStatus.textContent") == 1
    assert "@keyframes thinking-shimmer" in css
    # The overlay never covers the message it's waiting to answer.
    assert ".chat-stage:has(> .chat-thinking:not([hidden])) .chat-log" in css
    assert "prefers-reduced-motion" in css


def test_stop_gives_up_on_the_wait_without_moving_the_api(site):
    app_js = (site / "assets" / "app.js").read_text()
    assert "chatAbort = new AbortController();" in app_js
    assert "chatStop?.addEventListener('click', () => chatAbort?.abort());" in app_js
    # Both requests that show the overlay can be stopped: the question and a draft step.
    assert app_js.count("signal: beginChatRequest(),") == 2
    assert app_js.count("if (isStopped(err)) addChatMessage('note', 'Stopped.', false);") == 2
    # Stopping the first request on a served page must not be read as "this origin isn't the API".
    assert "if (speckyApiSettled || err.name === 'AbortError') throw err;" in app_js


def test_the_composer_grows_and_sends_on_enter(site):
    page = (site / "index.html").read_text()
    app_js = (site / "assets" / "app.js").read_text()
    assert '<textarea id="chat-input" rows="1"' in page
    assert "event.key !== 'Enter' || event.shiftKey || event.isComposing" in app_js
    assert "chatForm.requestSubmit();" in app_js
    # An open @ picker keeps Enter for itself.
    assert "if (mentionHits.length) return;" in app_js
    assert "chatSend.disabled = !chatInput.value.trim();" in app_js


def test_the_panel_opens_from_the_keyboard_and_esc_closes_it(site):
    page = (site / "index.html").read_text()
    app_js = (site / "assets" / "app.js").read_text()
    assert 'aria-keyshortcuts="Meta+. Control+."' in page
    assert "(event.metaKey || event.ctrlKey) && event.key === '.'" in app_js
    assert "event.key === 'Escape' && isAssistantOpen()" in app_js
    # Esc in the @ picker closes only the picker: it marks the key handled, and the panel checks.
    assert "if (event.defaultPrevented) return;" in app_js
    # Only the reader's own open slides the panel in, not a restore on the next page.
    assert "assistantPanel?.classList.add('is-entering');" in app_js


def test_an_empty_conversation_offers_ways_in(site):
    page = (site / "billing-refund-flow.html").read_text()
    app_js = (site / "assets" / "app.js").read_text()
    assert 'id="chat-empty"' in page and 'id="chat-suggestions"' in page
    assert "new MutationObserver(syncChatEmpty).observe(chatLog, { childList: true });" in app_js
    # A suggestion is text, and it only fills the composer — sending stays the reader's call.
    assert "button.textContent = text;" in app_js
    assert "button.addEventListener('click', () => fillComposer(text));" in app_js


def test_the_mention_picker_is_keyboard_driven_and_builds_no_markup(site):
    app_js = (site / "assets" / "app.js").read_text()
    mention_js = app_js[app_js.index("const mentionDropdown") : app_js.index("const tip =")]
    assert "innerHTML" not in mention_js
    assert "event.key === 'ArrowDown'" in mention_js
    assert "selectMention(mentionHits[mentionActive]);" in mention_js


def _css_rules(site) -> dict[str, str]:
    """site.css's top-level rules, selector -> declarations (comments dropped; a selector written
    twice keeps its last block)."""
    css = re.sub(r"/\*.*?\*/", "", (site / "assets" / "site.css").read_text(), flags=re.DOTALL)
    return dict(re.findall(r"^([^\s{}@][^{}]*?)\s*\{([^{}]*)\}", css, re.MULTILINE))


def test_glass_popovers_blur_the_page_not_just_the_container_they_hang_from(site):
    """An element with backdrop-filter is the backdrop root for everything inside it, so a glass
    popover nested in a glass container — the search results in the titlebar, the @ picker in the
    panel — blurs only that container's layer and shows the page through it, sharp. The containers
    keep their glass on ::before, which isn't anyone's ancestor."""
    rules = _css_rules(site)
    for container in (".titlebar", ".assistant-panel"):
        assert "backdrop-filter" not in rules[container]
        assert "backdrop-filter" in rules[f"{container}::before"]


def test_opening_the_assistant_collapses_the_nav_rail(site):
    page = (site / "index.html").read_text()
    css = (site / "assets" / "site.css").read_text()
    app_js = (site / "assets" / "app.js").read_text()
    titlebar = page[page.index('<div class="titlebar">') : page.index('<div class="body-row">')]
    assert 'id="nav-toggle"' in titlebar and 'aria-controls="nav-rail"' in titlebar
    assert 'id="nav-rail" class="sidebar"' in page
    assert "body.nav-collapsed .sidebar { display: none; }" in css
    assert "specky-nav-collapsed" in app_js
    # Closing the panel gives the rail back as it was before the panel opened.
    assert "specky-nav-before-assistant" in app_js


def test_an_answer_is_inserted_as_the_html_the_server_sanitized(site):
    app_js = (site / "assets" / "app.js").read_text()
    assert "div.innerHTML = data.answer_html || '';" in app_js
    # Live server responses are the only markup that reaches the log: an explore answer (its short
    # half and its details) and a finished draft. A replayed transcript is the stored markdown and
    # sessionStorage is editable by anything on the origin, so replay uses textContent — and a draft
    # step rebuilt from storage is built from data, never from markup.
    assert app_js.count("answer_html") == 2
    assert "details.innerHTML = data.details_html;" in app_js
    assert "body.innerHTML = data.answer_html || '';" in app_js
    assert "chat-rich" in (site / "assets" / "site.css").read_text()


def test_a_table_in_an_answer_wraps_between_words(site):
    """.chat-msg sets overflow-wrap: anywhere so a long path in prose can't push the panel wide, but
    anywhere also lets auto table layout count every letter as a break point when it sizes a column
    — short columns got squeezed until "Resolve range" read "Resolv / e range". Tables opt back into
    whole words; one too wide for the panel scrolls in its figure instead."""
    rules = _css_rules(site)
    assert "overflow-wrap: anywhere" in rules[".chat-msg"]
    figure = rules[".chat-rich figure.tw"]
    assert "overflow-wrap: break-word" in figure
    assert "overflow-x: auto" in figure


def test_an_explore_answer_keeps_its_details_behind_read_more(site):
    app_js = (site / "assets" / "app.js").read_text()
    css = (site / "assets" / "site.css").read_text()
    assert "if (data.details_html)" in app_js
    assert "more.textContent = 'Read more';" in app_js
    assert "more.setAttribute('aria-expanded', 'false');" in app_js
    assert ".chat-details[hidden] { display: none; }" in css


def test_a_draft_steps_through_the_panel_and_survives_navigation(site):
    page = (site / "index.html").read_text()
    app_js = (site / "assets" / "app.js").read_text()
    assert 'id="draft-reply"' in page and 'id="draft-cancel"' in page
    assert "speckyFetch('/draft'" in app_js
    assert "tabStore?.setItem('specky-draft'" in app_js
    for action in ("'choose'", "'confirm'", "'approve'", "'rescope'", "'reply'"):
        assert f"sendDraft({action}" in app_js
    # The step in progress is rebuilt on the next page from state — structured data, not markup.
    assert "renderDraftCard(draftState);" in app_js
    assert "innerHTML = draftState" not in app_js
    # "New" drops the draft along with the conversation.
    assert "clearDraft();" in app_js.split("chatReset?.addEventListener")[1]


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
