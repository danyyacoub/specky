import re

from specky.diagram_render import (
    _TAB_CSS,
    _TAB_TOKENS,
    DIAGRAM_CSS,
    DIAGRAM_JS,
    GLASS_DEFS,
    _scrub_svg,
)

# The seven variables beautiful-mermaid's SVG takes every color from (its src/theme.ts).
RENDERER_VARS = ("--bg", "--fg", "--line", "--accent", "--muted", "--surface", "--border")


# --- svg scrubbing ----------------------------------------------------------------------


def test_scrub_svg_strips_scripts_handlers_and_imports():
    svg = (
        '<svg width="100"><script>alert(1)</script>'
        '<style>@import url(http://evil/x.css);</style>'
        '<rect onclick="steal()" onload="x()"/>'
        "<foreignObject><b>x</b></foreignObject></svg>"
    )
    out = _scrub_svg(svg)
    assert "<script" not in out
    assert "@import" not in out
    assert "onclick" not in out and "onload" not in out
    assert "foreignObject" not in out
    assert '<svg width="100">' in out


def test_scrub_svg_leaves_clean_markup_alone():
    svg = '<svg width="20"><rect x="1" y="2"/></svg>'
    assert _scrub_svg(svg) == svg


# --- theming and glass ------------------------------------------------------------------


def test_the_viewer_repoints_every_variable_the_renderer_draws_with():
    """One left out falls back to beautiful-mermaid's own light value — a light-theme color on a
    dark page, which nothing anywhere would report."""
    rule = re.search(r"figure\.flow svg:not\(\.icon\) \{(.*?)\}", DIAGRAM_CSS, re.DOTALL).group(1)
    for var in RENDERER_VARS:
        assert re.search(rf"{var}: var\(--diagram-[\w-]+\) !important;", rule), var


def test_every_diagram_token_the_rules_use_is_declared():
    declared = set(re.findall(r"(--diagram-[\w-]+)\s*:", DIAGRAM_CSS))
    used = set(re.findall(r"var\((--diagram-[\w-]+)\)", DIAGRAM_CSS + DIAGRAM_JS + GLASS_DEFS))
    assert used and used <= declared, used - declared


def test_the_full_screen_tab_is_handed_every_token_it_reads():
    """The tab is a Blob page with no site.css: a token its CSS reads that it isn't handed is unset
    there, and the diagram quietly loses that color."""
    page = DIAGRAM_JS.split("function diagramPage", 1)[1].split("function addDiagramButtons", 1)[0]
    css = re.sub(r'\[fill="[^"]*"\]', "", _TAB_CSS + page)  # the renderer's own, in selectors
    used = set(re.findall(r"var\((--[\w-]+)\)", css))
    assert used and used <= set(_TAB_TOKENS), used - set(_TAB_TOKENS)


def test_every_glass_reference_resolves_to_a_definition_that_paints():
    ids = set(re.findall(r'id="([\w-]+)"', GLASS_DEFS))
    refs = set(re.findall(r"url\(#([\w-]+)\)", DIAGRAM_CSS + _TAB_CSS))
    assert refs and refs <= ids, refs - ids
    # A gradient or filter defined inside a `display:none` <svg> is never painted — which is why
    # these live beside html_render's icon sprite rather than in it.
    assert "display:none" not in GLASS_DEFS.replace(" ", "")
