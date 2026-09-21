"""Diagrams: a ```mermaid``` fence rendered to a static `<svg>`, and how the viewer shows one.

A fence is rendered at `render-html` time (via the mermaid-render Node tool wrapping
`beautiful-mermaid` — see mermaid_tool.py for which copy of it gets used) rather than shipping a
diagram-rendering library to every reader. If Node or that tool's dependencies aren't set up, the
fenced source is left as plain-text fallback rather than failing the whole render — same as any doc
with no diagrams at all.

Everything diagram-shaped lives here rather than in html_render.py: the render itself, the scrub
that keeps its output inert, the CSS that frames it and the full-screen view's JS. html_render.py
stitches `DIAGRAM_CSS` and `DIAGRAM_JS` into the site's shared assets and calls
`render_mermaid_blocks` for a doc page; answer_render.py calls it too, bounded, for the Spec Assistant.

Like mermaid_tool.py, this imports nothing but the standard library — a diagram is a subprocess
and some regexes, and shouldn't pay for markdown/jinja to be one.
"""

from __future__ import annotations

import html
import json
import re
import subprocess

from specky import mermaid_tool

# --- diagrams: fenced ```mermaid``` -> static <svg> via the mermaid-render Node tool ----
# Which copy of that tool gets used is `mermaid_tool`'s problem, not this module's: an installed
# specky runs it out of ~/.cache/specky, a checkout out of vendor/. See that module's docstring.

_MERMAID_BLOCK = re.compile(r'<pre><code class="language-mermaid">(.*?)</code></pre>', re.DOTALL)
_SVG_ROOT_WIDTH = re.compile(r'<svg[^>]*\swidth="([\d.]+)"')
_SVG_IMPORT = re.compile(r"@import[^;]*;")
_SVG_DANGEROUS_TAG = re.compile(r"<(script|foreignObject|iframe)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_SVG_EVENT_ATTR = re.compile(r'\s+on\w+="[^"]*"', re.IGNORECASE)

# The colors a diagram carries in its own markup, copied by hand from html_render's light `:root`
# tokens (this runs server-side, with no `getComputedStyle` to read them back from). They are the
# fallback, not what a viewer reader sees: the viewer re-points the same seven variables at its
# live tokens (see DIAGRAM_CSS), so a diagram follows the reader's light/dark scheme there. What
# reads these is everything without that stylesheet — the `specky export` page and its PDF — and
# the xychart series palette, which beautiful-mermaid derives from the `accent` hex in JS rather
# than through a CSS variable.
#
# Spacing is the library's own default (nodeSpacing 24, layerSpacing 40); only the canvas padding
# is below its default of 40, because the figure around the SVG already pads it. Tighter values
# made boxes crowd each other for ~20px of width on the repo's widest diagram.
MERMAID_THEME = {
    "fg": "#1d1f23",  # --text-primary
    "line": "#63666d",  # --text-secondary
    "accent": "#0166ff",  # --accent
    "muted": "#63666d",  # --text-secondary
    "surface": "#ffffff",  # --surface
    "border": "#e1e3e8",  # --border
    "font": "Helvetica",  # generic system sans; --font-display (Poppins) is for headings only
    "transparent": True,
    "padding": 12,
    "nodeSpacing": 24,
    "layerSpacing": 40,
}
# Past this, a diagram that doesn't fit its pane keeps its natural size and scrolls sideways
# instead of being shrunk to fit; shrunk from any wider, its labels stop being readable. It only
# decides the case where a diagram doesn't fit (one that fits shows at natural size either way),
# and it is shared by the doc column (up to 1040 - 2*48 = 944px) and the Spec Assistant panel (320-720px),
# so it is a property of the diagram, not of either pane.
WIDE_DIAGRAM_PX = 600


def _scrub_svg(svg: str) -> str:
    """Belt-and-braces cleanup, mirroring glia's `scrubSvg` (renderDiagrams.ts).

    `beautiful-mermaid`'s own output carries neither scripts nor remote references in
    practice — this is the second half of the same bargain glia's frontend makes: the
    renderer is trusted, but a page that promises it makes no third-party request and
    runs no injected script should stay true even if a future version of a dependency
    decides to emit one.
    """
    svg = _SVG_DANGEROUS_TAG.sub("", svg)
    svg = _SVG_EVENT_ATTR.sub("", svg)
    svg = _SVG_IMPORT.sub("", svg)
    return svg


def render_mermaid_svg(source: str) -> str | None:
    """One mermaid source string rendered to a scrubbed `<svg>`, or `None`.

    `None` covers every reason this can't happen — Node missing, the tool's dependencies never
    installed (`specky setup-diagrams`), or the source itself failing to parse — and the caller's
    response to all of them is the same: leave the fenced source as readable text rather
    than failing the render.
    """
    tool_dir = mermaid_tool.tool_dir()
    if tool_dir is None:
        return None
    payload = json.dumps({"source": source, "options": MERMAID_THEME})
    try:
        proc = subprocess.run(
            ["node", "render.mjs"],
            input=payload,
            capture_output=True,
            text=True,
            cwd=tool_dir,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return _scrub_svg(proc.stdout)


def render_mermaid_blocks(body_html: str, limit: int | None = None) -> tuple[str, bool, bool]:
    """Replace fenced mermaid blocks with rendered `<figure class="flow">` SVGs.

    Returns `(html, any_mermaid_source, any_rendered)` — the first two counts drive
    `render_site()`'s one-time hint if diagrams exist but none could be rendered (Node or
    the tool's `node_modules` missing).

    `limit` caps how many fences are actually rendered; the rest are left as their own source
    text. A doc page passes None (a doc's diagrams are written by a person and reviewed in git),
    while a chat answer bounds it — each fence is a `node` subprocess, so an answer full of them
    would be one question spawning a dozen (see `answer_render.MAX_ANSWER_DIAGRAMS`).
    """
    any_source = False
    any_rendered = False
    drawn = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal any_source, any_rendered, drawn
        any_source = True
        if limit is not None and drawn >= limit:
            return match.group(0)
        drawn += 1
        svg = render_mermaid_svg(html.unescape(match.group(1)))
        if svg is None:
            return match.group(0)
        any_rendered = True
        width_match = _SVG_ROOT_WIDTH.search(svg)
        wide = width_match is not None and float(width_match.group(1)) > WIDE_DIAGRAM_PX
        inner = f'<div class="fx">{svg}</div>' if wide else svg
        return f'<figure class="flow">{inner}</figure>'

    return _MERMAID_BLOCK.sub(repl, body_html), any_source, any_rendered


# --- how a diagram looks in the viewer ------------------------------------------------------

# The diagram palette, as aliases of html_render's `:root` tokens. Declared on :root and only
# there, so each resolves against whichever scheme the page is in: the dark block that redefines
# --surface, --border and the rest redefines these along with them.
_TOKENS_CSS = """
:root {
  /* The page's own color, so a diagram sits in the page rather than in a gray box. */
  --diagram-bg: var(--surface);
  --diagram-node: var(--surface);
  --diagram-stroke: var(--border);
  --diagram-text: var(--text-primary);
  --diagram-muted: var(--text-secondary);
  --diagram-line: var(--text-secondary);
  /* Arrowheads in the line's gray, not the accent blue MERMAID_THEME gives them: on this site blue
     means "you can click this". `var(--accent)` here brings the blue back. A chart's bars and
     lines draw with the same variable, and keep the accent (--diagram-chart). */
  --diagram-arrow: var(--text-secondary);
  --diagram-chart: var(--accent);
  /* The label on a box its author colored — the light theme's text color in both schemes, see
     the rule that uses it. */
  --diagram-text-on-fill: #1d1f23;
  /* The glass: a pane's sheen top to bottom, a subgraph's fainter pane, a header band's tint and
     the hairline edge. Then the two shadows that lift a box off the backdrop: a tight one where
     it meets the surface, and a softer one further out. */
  --diagram-glass-hi: rgb(255 255 255 / 0.72);
  --diagram-glass-lo: rgb(255 255 255 / 0.42);
  --diagram-glass-group: rgb(255 255 255 / 0.3);
  --diagram-glass-tint: rgb(29 31 35 / 0.04);
  --diagram-glass-edge: rgb(29 31 35 / 0.1);
  --diagram-shadow-near: rgb(15 23 42 / 0.14);
  --diagram-shadow: rgb(15 23 42 / 0.1);
  /* What the glass sits on. A translucent pane over a flat fill only reads as a paler box, so the
     figure gets a few soft washes of the site's own colors, faint enough to stay behind the text.
     Mixed from the tokens rather than fixed, so each follows the scheme. */
  --diagram-backdrop:
    radial-gradient(70% 60% at 0% 0%, color-mix(in srgb, var(--accent) 4%, transparent), transparent 70%),
    radial-gradient(60% 55% at 100% 100%, color-mix(in srgb, var(--feature) 3%, transparent), transparent 70%),
    radial-gradient(50% 50% at 100% 0%, color-mix(in srgb, var(--tag-0) 3%, transparent), transparent 70%),
    var(--diagram-bg);
}
@media (prefers-color-scheme: dark) {
  :root {
    --diagram-glass-hi: rgb(255 255 255 / 0.1);
    --diagram-glass-lo: rgb(255 255 255 / 0.04);
    --diagram-glass-group: rgb(255 255 255 / 0.025);
    --diagram-glass-tint: rgb(255 255 255 / 0.05);
    --diagram-glass-edge: rgb(255 255 255 / 0.14);
    --diagram-shadow-near: rgb(0 0 0 / 0.55);
    --diagram-shadow: rgb(0 0 0 / 0.4);
  }
}
/* A reader who has asked for less transparency, or more contrast, gets solid panes on a plain
   backdrop: the same diagram without the glass. The shadows stay — they say "raised", not "see
   through". After the dark block, so it wins in both. */
@media (prefers-reduced-transparency: reduce), (prefers-contrast: more) {
  :root {
    --diagram-glass-hi: var(--diagram-node);
    --diagram-glass-lo: var(--diagram-node);
    --diagram-glass-group: var(--surface-secondary);
    --diagram-glass-tint: var(--surface-tertiary);
    --diagram-glass-edge: var(--diagram-stroke);
    --diagram-backdrop: var(--diagram-bg);
  }
}
"""

# The two places a diagram's <svg> gets styled: inline in a viewer page (a doc or the Spec Assistant panel),
# and alone in the full-screen tab DIAGRAM_JS opens, which has no site.css. `_svg_rules` is written
# once and scoped to each, so the tab can't drift from the page.
_SVG_IN_PAGE = "figure.flow svg:not(.icon)"
_SVG_IN_TAB = "#stage > svg"


def _svg_rules(svg: str) -> str:
    """The rules that theme and glaze a diagram <svg>, for the selector `svg`."""
    return f"""
/* beautiful-mermaid writes its seven variables inline on the <svg> (from MERMAID_THEME), and only
   `!important` lets a stylesheet beat an inline style. That is why it's done this way: the SVG
   keeps its light values, so it stays whole anywhere this stylesheet isn't (the export, a PDF),
   and here it is re-pointed at the site's tokens. Through --diagram-* aliases rather than
   directly, because `--surface: var(--surface)` on one element is a cycle, not a lookup. */
{svg} {{
  --bg: var(--diagram-bg) !important; --fg: var(--diagram-text) !important;
  --line: var(--diagram-line) !important; --accent: var(--diagram-arrow) !important;
  --muted: var(--diagram-muted) !important; --surface: var(--diagram-node) !important;
  --border: var(--diagram-stroke) !important;
}}
{svg}[data-xychart-colors] {{ --accent: var(--diagram-chart) !important; }}
/* Glass. Whatever the diagram type, beautiful-mermaid fills every box through one variable —
   flowchart and state nodes, sequence actors, class and ER boxes all paint --_node-fill — so
   matching that attribute reaches them all, and a stylesheet beats a presentation attribute
   without touching the SVG. `backdrop-filter` does nothing to an SVG shape, so the glass is
   imitated rather than blurred: a translucent sheen (#diagram-glass, GLASS_DEFS) over the
   figure's tinted backdrop, a hairline edge and a soft shadow. A sequence note is the same kind
   of pane, painted --bg. */
{svg} [fill="var(--_node-fill)"], {svg} .note > [fill="var(--bg)"] {{
  fill: url(#diagram-glass); stroke: var(--diagram-glass-edge); filter: url(#diagram-glass-shadow);
}}
/* Class/ER header bands and subgraph title bars: a tint over the pane under them. */
{svg} [fill="var(--_group-hdr)"] {{ fill: var(--diagram-glass-tint); stroke: var(--diagram-glass-edge); }}
/* A subgraph is a fainter pane with no shadow, so the nodes on it read as glass on glass. */
{svg} [fill="var(--_group-fill)"] {{ fill: var(--diagram-glass-group); stroke: var(--diagram-glass-edge); }}
/* An edge label sits on its edge, so its chip stays opaque: a line through a label costs more
   than the glass is worth. */
{svg} .edge-label > rect {{ fill: var(--diagram-node); stroke: var(--diagram-glass-edge); }}
/* A box its author colored (`classDef` or `style`, which beautiful-mermaid writes straight into
   `fill`, so it is no var(...)) keeps that color and no glass. Its label keeps the light theme's
   text color: the fill was picked against the light diagram MERMAID_THEME renders, and the dark
   scheme's light text would all but vanish on it. A label the author colored too is left alone. */
{svg} .node:has(> [fill]:not(text):not([fill^="var("])) > text[fill^="var("] {{
  fill: var(--diagram-text-on-fill);
}}
/* It is raised like any other box, though. */
{svg} .node > [fill]:not(text):not([fill^="var("]) {{ filter: url(#diagram-glass-shadow); }}
/* Rounded corners. beautiful-mermaid draws a plain box square (rx="0") and mermaid's rounded box
   `( )` at rx="6"; both are rounder here, the rounded one still the rounder of the two so the
   difference an author drew survives. A stadium or a circle has a radius of its own and keeps it;
   a diamond or a hexagon is a polygon, which no radius reaches. CSS `rx`/`ry` beat the attributes. */
{svg} :is(.node, .class-node, .entity) > rect[rx="0"]:not([fill="var(--_group-hdr)"]),
{svg} .actor > rect {{ rx: 8px; ry: 8px; }}
{svg} .node > rect[rx="6"] {{ rx: 14px; ry: 14px; }}
{svg} .subgraph > rect[rx="0"]:not([fill="var(--_group-hdr)"]) {{ rx: 12px; ry: 12px; }}
/* A header band lies across the top of its box or subgraph, so it is rounded at the top only, to
   follow the corners under it — by a clip, since `rx` would round its bottom corners as well. */
{svg} :is(.class-node, .entity) > rect[fill="var(--_group-hdr)"] {{ clip-path: inset(0 round 8px 8px 0 0); }}
{svg} .subgraph > rect[fill="var(--_group-hdr)"] {{ clip-path: inset(0 round 12px 12px 0 0); }}
/* Edge-label chips (flowchart and ER). */
{svg} rect[rx="2"][fill="var(--bg)"] {{ rx: 4px; ry: 4px; }}
"""


# The gradient and shadow the glass rules reference by `url(#…)`. html_render puts this in every
# page once, beside its icon sprite — but not inside it: the sprite is `display:none`, and a
# browser doesn't paint a gradient or filter defined in a hidden <svg>, so this one is zero-sized
# instead. Stop and flood colors are CSS properties, so they read the scheme's tokens like any rule.
GLASS_DEFS = (
    '<svg id="diagram-glass-defs" width="0" height="0" style="position:absolute" '
    'aria-hidden="true" focusable="false"><defs>'
    '<linearGradient id="diagram-glass" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0" style="stop-color: var(--diagram-glass-hi)"/>'
    '<stop offset="1" style="stop-color: var(--diagram-glass-lo)"/>'
    "</linearGradient>"
    # Two shadows, a tight one and a soft one, cast by the shape's silhouette rather than by the
    # shape: a glass pane is mostly transparent, so its own alpha would cast a faint shadow — and
    # one that shows through the pane, clouding it. So the alpha is pushed to solid first (a pane
    # at 4% opacity still counts as the whole box), and whatever falls under the box is cut away
    # after, which is how a CSS box-shadow behaves. The region leaves room below for the soft one
    # to fall into; the default 10% margin clips it under a short, wide node.
    '<filter id="diagram-glass-shadow" x="-20%" y="-50%" width="140%" height="200%" '
    'color-interpolation-filters="sRGB">'
    '<feComponentTransfer in="SourceAlpha" result="solid">'
    '<feFuncA type="linear" slope="100"/></feComponentTransfer>'
    '<feGaussianBlur in="solid" stdDeviation="3" result="far-blur"/>'
    '<feOffset in="far-blur" dy="3" result="far-offset"/>'
    '<feFlood style="flood-color: var(--diagram-shadow)"/>'
    '<feComposite in2="far-offset" operator="in" result="far"/>'
    '<feGaussianBlur in="solid" stdDeviation="0.6" result="near-blur"/>'
    '<feOffset in="near-blur" dy="1" result="near-offset"/>'
    '<feFlood style="flood-color: var(--diagram-shadow-near)"/>'
    '<feComposite in2="near-offset" operator="in" result="near"/>'
    '<feMerge result="both"><feMergeNode in="far"/><feMergeNode in="near"/></feMerge>'
    '<feComposite in="both" in2="solid" operator="out" result="outside"/>'
    '<feMerge><feMergeNode in="outside"/><feMergeNode in="SourceGraphic"/></feMerge>'
    "</filter></defs></svg>"
)

DIAGRAM_CSS = (
    _TOKENS_CSS
    + """
/* --- diagrams: a static <svg> from vendor/mermaid-render, pre-rendered once server-side. The
   figure is the tinted backdrop its glass panes sit on (--diagram-backdrop). No border: the
   backdrop is the page's own color, so the diagram sits in the page rather than framed on it. */
figure.flow {
  position: relative; border-radius: var(--radius-md);
  background: var(--diagram-backdrop); text-align: center;
}
figure.flow svg { max-width: 100%; height: auto; }
/* A diagram too wide to shrink readably keeps its size and scrolls sideways instead. */
figure.flow .fx { overflow-x: auto; }
figure.flow .fx svg { max-width: none; margin: 0; }
.doc figure.flow { margin: 20px 0; padding: 16px; }
"""
    + _svg_rules(_SVG_IN_PAGE)
    + """
/* The "open full screen" button DIAGRAM_JS adds to every diagram: out of the way until the
   reader is on the diagram, always there for keyboard focus. The site's glass material, as on
   .titlebar — it floats over the diagram, so here the blur has real content under it. */
.flow-open {
  position: absolute; top: 6px; right: 6px; display: flex; align-items: center; gap: 4px;
  background: var(--glass-bg); backdrop-filter: blur(20px) saturate(160%);
  -webkit-backdrop-filter: blur(20px) saturate(160%); border: 1px solid var(--glass-border);
  box-shadow: 0 1px 4px rgb(0 0 0 / 0.1); color: var(--text-secondary); font: inherit;
  font-size: 0.6875rem; padding: 3px 8px; border-radius: var(--radius-sm); cursor: pointer;
  opacity: 0; transition: opacity 0.15s;
}
figure.flow:hover .flow-open, .flow-open:focus-visible { opacity: 1; }
.flow-open:hover { color: var(--text-primary); }
/* Beats `figure.flow svg`, which would otherwise size the button's icon like a diagram. */
figure.flow .flow-open .icon { width: 1em; height: 1em; max-width: none; }

.chat-rich figure.flow { margin: 12px 0; padding: 12px; max-width: 100%; box-sizing: border-box; }
/* In the panel, the scroll stays inside it rather than widening it. */
.chat-rich figure.flow .fx { max-width: 100%; }
"""
)

# Everything the full-screen tab is handed from the page it was opened from: every --diagram-*
# token (read off _TOKENS_CSS, so the list can't fall behind it) and what its hint pill is made of.
_TAB_TOKENS = sorted(set(re.findall(r"(--diagram-[\w-]+):", _TOKENS_CSS))) + [
    "--glass-bg",
    "--glass-border",
    "--text-secondary",
]
# The tab's copy of the SVG rules, comments dropped: they ship inside app.js, not a stylesheet.
_TAB_CSS = re.sub(r"/\*.*?\*/\n?", "", _svg_rules(_SVG_IN_TAB), flags=re.DOTALL)

# Every rendered diagram gets a button that opens it alone in a new tab, fitted to the window,
# with wheel zoom, drag to pan and double-click to reset — a wide flowchart that is a scroll strip
# in the doc column is readable whole there. The page is a Blob URL built from the SVG already
# inline in this page: no extra file per diagram at render time, and it works on file:// too. The
# SVG is the renderer's own scrubbed output (see `_scrub_svg`), the same markup this page shows.
# The tab has no site.css, so it is handed the tokens as they resolve here — the reader's
# light/dark scheme included — along with this page's GLASS_DEFS and its own copy of the SVG rules.
# Not standalone: it runs inside html_render's app.js, and uses SEARCH_JS's `escapeHtml` and the
# `#icon-expand` symbol from ICON_SPRITE.
DIAGRAM_JS = (
    f"const DIAGRAM_TAB_TOKENS = {json.dumps(_TAB_TOKENS)};\n"
    f"const DIAGRAM_TAB_CSS = {json.dumps(_TAB_CSS)};\n"
    + """
function diagramPage(svg, title) {
  const computed = getComputedStyle(document.documentElement);
  const tokens = DIAGRAM_TAB_TOKENS
    .map((name) => name + ': ' + computed.getPropertyValue(name).trim() + ';')
    .join(' ');
  const defs = document.getElementById('diagram-glass-defs')?.outerHTML ?? '';
  return `<!doctype html><html><head><meta charset="utf-8"><title>${escapeHtml(title)}</title>
<style>
  :root { ${tokens} }
  html, body {
    margin: 0; height: 100%; overflow: hidden; background: var(--diagram-backdrop);
    font: 12px system-ui, sans-serif;
  }
  #stage { position: absolute; inset: 0; cursor: grab; user-select: none; }
  #stage.dragging { cursor: grabbing; }
  #stage svg { position: absolute; left: 0; top: 0; transform-origin: 0 0; max-width: none; }
  #hint {
    position: fixed; bottom: 10px; left: 50%; transform: translateX(-50%); color: var(--text-secondary);
    background: var(--glass-bg); backdrop-filter: blur(20px) saturate(160%);
    -webkit-backdrop-filter: blur(20px) saturate(160%); border: 1px solid var(--glass-border);
    border-radius: 6px; padding: 3px 10px;
  }
${DIAGRAM_TAB_CSS}
</style></head>
<body>${defs}<div id="stage">${svg}</div>
<div id="hint">Scroll to zoom \\u00b7 drag to pan \\u00b7 double-click to fit</div>
<script>
const stage = document.getElementById('stage');
const svg = stage.querySelector('svg');
const { width, height } = svg.viewBox.baseVal;
let scale = 1, panX = 0, panY = 0, drag = null;

function draw() {
  svg.style.transform = 'translate(' + panX + 'px, ' + panY + 'px) scale(' + scale + ')';
}
function fit() {
  const pad = 32;
  scale = Math.min((innerWidth - 2 * pad) / width, (innerHeight - 2 * pad) / height);
  panX = (innerWidth - width * scale) / 2;
  panY = (innerHeight - height * scale) / 2;
  draw();
}

// Zoom about the cursor: the point under it stays where it is.
stage.addEventListener('wheel', (event) => {
  event.preventDefault();
  const factor = Math.exp(-event.deltaY * 0.0015);
  panX = event.clientX - (event.clientX - panX) * factor;
  panY = event.clientY - (event.clientY - panY) * factor;
  scale *= factor;
  draw();
}, { passive: false });
stage.addEventListener('pointerdown', (event) => {
  drag = { x: event.clientX - panX, y: event.clientY - panY };
  stage.classList.add('dragging');
  stage.setPointerCapture(event.pointerId);
});
stage.addEventListener('pointermove', (event) => {
  if (!drag) return;
  panX = event.clientX - drag.x;
  panY = event.clientY - drag.y;
  draw();
});
stage.addEventListener('pointerup', () => { drag = null; stage.classList.remove('dragging'); });
stage.addEventListener('dblclick', fit);
addEventListener('resize', fit);
fit();
<\\/script></body></html>`;
}

function addDiagramButtons(root) {
  for (const figure of root.querySelectorAll('figure.flow')) {
    if (figure.querySelector(':scope > .flow-open')) continue;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'flow-open';
    button.title = 'Open this diagram full screen in a new tab';
    button.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#icon-expand"></use></svg>Full screen';
    figure.appendChild(button);
  }
}

document.addEventListener('click', (event) => {
  const button = event.target.closest?.('.flow-open');
  if (!button) return;
  const svg = button.closest('figure.flow')?.querySelector('svg:not(.icon)');
  if (!svg) return;
  const blob = new Blob([diagramPage(svg.outerHTML, document.title)], { type: 'text/html' });
  window.open(URL.createObjectURL(blob), '_blank', 'noopener');
});

addDiagramButtons(document);
"""
)
