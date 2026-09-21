"""Static HTML doc site: a searchable, file://-browsable view of the specs/ tree for
non-technical readers. No build step for the *reader* (fonts/JS fall back to system
stacks) — the whole site is just files you can open directly or zip up and send someone.

The `file://` promise is what shapes the asset layout. What a `file://` page can't do is
`fetch()`/`XMLHttpRequest` (every local file is its own opaque origin), so the search index
can't be loaded as JSON at runtime; what it *can* do is load a relative `<link href>` and
`<script src>`. So the CSS, the JS, the search index and the glossary hover map all live in
`assets/` — written once, shared by every page, no server and no build involved. Inlining
them into every page instead cost ~45 KB of byte-identical duplication per page (a 2.0 MB
site for 37 docs, against ~600 KB now). What stays inline per page is what actually differs:
the doc body and the icon sprite, the latter because a cross-file `<use href="icons.svg#id">`
*is* blocked on `file://`.

One thing reaches outside a page itself: the optional chat widget, which POSTs to a local
`specky serve` companion on 127.0.0.1 (see chat_server.py) — browsing and search work
identically whether or not that server is running.

A ```mermaid``` fence in a doc is rendered to a plain static `<svg>` at `render-html`
time rather than shipping a diagram-rendering library to every reader — see diagram_render.py,
which owns that render and the CSS/JS that frame it.

Glossary terms from `specs/GLOSSARY.md` are auto-linked to a hover tooltip on first
mention per page, and tables get a scrollable, zebra-striped treatment — both patterns
(and the diagram approach above) ported from the user's `glia` project
(`apps/backend/app/domains/docs/enrichment_render.py`), adapted from that project's
sandboxed-iframe-hosted pages to specky's plain self-contained ones.

The shell is a titlebar + sidebar + content pane (closer to a native desktop app than a
marketing page): colors are semantic tokens with light and dark variants (system-driven,
no in-app toggle), a single blue accent reserved for interactive/link meaning, and a
neutral gray ramp capped at a dark gray (never pure black) for chrome. `doc_type`
(feature/workflow) and `tags` — both already in the DB (see db.py) but previously unused
here — render as colored chips that double as client-side filters over the sidebar nav
and search results; `related` (also previously unused) renders as a per-page "Related"
section, giving real doc-to-doc navigation beyond the domain-grouped list. A small
hand-built inline SVG icon sprite (one consistent outline weight) covers navigation,
tags/types, and the chat widget — no icon font, no new dependency.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
from pathlib import Path

import markdown as md
from jinja2 import Environment

from specky import diagram_render, paths
from specky.chat_server import DEFAULT_PORT as CHAT_PORT
from specky.db import connect
from specky.staleness import days_behind

_env = Environment(autoescape=True)

# --- icons: hand-built inline SVG symbols, referenced via <use> so every icon is one
# small <symbol> definition shared across the whole page rather than repeated markup.
# Deliberately geometric (lines/circles/rects/polygons) rather than freehand paths, so
# they render correctly without a design tool. Pure stroke outlines, no fills, so a
# single `.icon { fill: none; stroke: currentColor }` rule covers all of them.
ICON_SPRITE = """
<svg style="display:none" aria-hidden="true">
<symbol id="icon-brand" viewBox="0 0 20 20"><line x1="10" y1="2" x2="10" y2="18"/><line x1="3.1" y1="6" x2="16.9" y2="14"/><line x1="3.1" y1="14" x2="16.9" y2="6"/></symbol>
<symbol id="icon-search" viewBox="0 0 20 20"><circle cx="9" cy="9" r="6"/><line x1="13.5" y1="13.5" x2="18" y2="18"/></symbol>
<symbol id="icon-home" viewBox="0 0 20 20"><polyline points="3,10 10,3 17,10"/><path d="M5 9.5 V17 H15 V9.5"/><rect x="8.5" y="12.5" width="3" height="4.5"/></symbol>
<symbol id="icon-chat" viewBox="0 0 20 20"><rect x="3" y="4" width="14" height="10" rx="2.5"/><polyline points="7,14 7,17 10.5,14"/></symbol>
<symbol id="icon-terminal" viewBox="0 0 20 20"><rect x="3" y="4" width="14" height="12" rx="1.5"/><polyline points="6.5,8.5 9.5,10.5 6.5,12.5"/><line x1="10.5" y1="13.5" x2="14" y2="13.5"/></symbol>
<symbol id="icon-book" viewBox="0 0 20 20"><rect x="3" y="4" width="14" height="13" rx="1"/><line x1="10" y1="4" x2="10" y2="17"/><line x1="5" y1="7.5" x2="8" y2="7.5"/><line x1="5" y1="10.5" x2="8" y2="10.5"/><line x1="12" y1="7.5" x2="15" y2="7.5"/><line x1="12" y1="10.5" x2="15" y2="10.5"/></symbol>
<symbol id="icon-file-text" viewBox="0 0 20 20"><path d="M6 3 H12 L16 7 V17 H6 Z"/><polyline points="12,3 12,7 16,7"/><line x1="8" y1="10.5" x2="14" y2="10.5"/><line x1="8" y1="13" x2="14" y2="13"/></symbol>
<symbol id="icon-clock" viewBox="0 0 20 20"><circle cx="10" cy="10" r="7"/><line x1="10" y1="10" x2="10" y2="6"/><line x1="10" y1="10" x2="13" y2="11.5"/></symbol>
<symbol id="icon-image" viewBox="0 0 20 20"><rect x="3" y="4" width="14" height="12" rx="1"/><circle cx="7.5" cy="8.5" r="1.4"/><polyline points="4,15 8,10.5 11,13 14,9.5 17,12.5"/></symbol>
<symbol id="icon-folder" viewBox="0 0 20 20"><path d="M3 6 H8 L9.5 8 H17 V16 H3 Z"/></symbol>
<symbol id="icon-sparkle" viewBox="0 0 20 20"><polygon points="10,3 12,8 17,10 12,12 10,17 8,12 3,10 8,8"/></symbol>
<symbol id="icon-cycle" viewBox="0 0 20 20"><path d="M4 10 A6 6 0 0 1 10 4 H13"/><polyline points="11,2 13,4 11,6"/><path d="M16 10 A6 6 0 0 1 10 16 H7"/><polyline points="9,18 7,16 9,14"/></symbol>
<symbol id="icon-link" viewBox="0 0 20 20"><line x1="7" y1="13" x2="13" y2="7"/><polyline points="9,7 13,7 13,11"/></symbol>
<symbol id="icon-person" viewBox="0 0 20 20"><circle cx="10" cy="7" r="3.2"/><path d="M4 17 A6 6 0 0 1 16 17"/></symbol>
<symbol id="icon-expand" viewBox="0 0 20 20"><polyline points="12,3 17,3 17,8"/><line x1="17" y1="3" x2="11.5" y2="8.5"/><polyline points="8,17 3,17 3,12"/><line x1="3" y1="17" x2="8.5" y2="11.5"/></symbol>
<symbol id="icon-send" viewBox="0 0 20 20"><polygon points="3,10 17,4 12,17 9,11"/><line x1="9" y1="11" x2="17" y2="4"/></symbol>
<symbol id="icon-sidebar" viewBox="0 0 20 20"><rect x="3" y="4" width="14" height="12" rx="1.5"/><line x1="8" y1="4" x2="8" y2="16"/></symbol>
</svg>
"""

# A doc's type, as an icon: the type chips, the sidebar links and (via SEARCH_JS, which is handed
# this map) the search results all draw from it.
_TYPE_ICONS = {"feature": "sparkle", "workflow": "cycle"}
_TYPE_ICON_FALLBACK = "file-text"

_RAIL_TEMPLATE = _env.from_string(
    '<div id="nav-rail" class="sidebar">'
    "{% for d in domains %}"
    '<details class="domain-group"{{ " open" if d.open else "" }}>'
    '<summary><svg class="icon" aria-hidden="true">'
    '<use href="#icon-{{ d.icon }}"></use></svg>{{ d.name }}</summary><ul>'
    "{% for doc in d.docs %}"
    # No "active" class here: the rail is rendered once for the whole site, so NAV_JS marks
    # the current page from location.pathname instead.
    # `nav_title` drops the module prefix the group header already shows; the full title stays
    # on hover.
    '<li><a href="{{ doc.html_name }}" title="{{ doc.title }}" data-tags="{{ doc.data_tags }}" '
    'data-type="{{ doc.doc_type }}" data-stale="{{ doc.stale }}"><svg class="icon" '
    'aria-hidden="true"><use href="#icon-{{ doc.type_icon }}"></use></svg>'
    "<span>{{ doc.nav_title }}</span></a></li>"
    "{% endfor %}</ul></details>"
    "{% endfor %}"
    "{% if has_features or has_workflows or has_stale or all_tags %}"
    '<div class="tag-filter"><div class="filter-title"><span>Filter</span>'
    '<button id="clear-filter" class="clear-filter" type="button" hidden>Clear</button></div>'
    '<div class="chip-row">'
    '{% if has_features %}<button class="chip type-feature" type="button" data-facet="type" '
    'data-type="feature" data-active="false"><svg class="icon" aria-hidden="true">'
    '<use href="#icon-sparkle"></use></svg>Feature</button>{% endif %}'
    '{% if has_workflows %}<button class="chip type-workflow" type="button" data-facet="type" '
    'data-type="workflow" data-active="false"><svg class="icon" aria-hidden="true">'
    '<use href="#icon-cycle"></use></svg>Workflow</button>{% endif %}'
    '{% if has_stale %}<button class="chip stale" type="button" data-facet="stale" '
    'data-active="false"><svg class="icon" aria-hidden="true">'
    '<use href="#icon-clock"></use></svg>Stale</button>{% endif %}'
    "{% for t in all_tags %}"
    '<button class="chip {{ t.cls }}" type="button" data-facet="tag" data-tag="{{ t.name }}" '
    'data-active="false">{{ t.name }}</button>'
    "{% endfor %}</div></div>"
    "{% endif %}"
    "</div>"
)

# The Spec Assistant panel is a column of the page, not a popover floating over it: an answer
# grounded in whole docs (tables, a diagram, a drafted spec) needs a doc's worth of room, and a 340px
# bubble was reading a document through a keyhole. It's the third flex child of `.body-row`, so opening it
# reflows the content pane rather than covering it — nav stays reachable mid-conversation, and the
# reader can follow a cited source without losing the panel.
_ASSISTANT_PANEL = (
    '<aside id="assistant-panel" class="assistant-panel" aria-label="Spec Assistant">'
    '<div id="assistant-resize" class="assistant-resize" role="separator" '
    'aria-orientation="vertical" aria-label="Resize panel" tabindex="0"></div>'
    '<div class="assistant-body">'
    '<div class="chat-header">'
    '<span class="chat-title"><svg class="icon" aria-hidden="true">'
    '<use href="#icon-chat"></use></svg>Spec Assistant</span>'
    '<span class="chat-header-right">'
    '<button id="chat-reset" class="chat-reset" type="button">New</button>'
    '<button id="assistant-close" class="assistant-close" type="button" aria-label="Close panel">'
    "&#215;</button></span></div>"
    '<div id="chat-log" class="chat-log"></div>'
    # Shown while a draft is waiting on the reader: what they type next answers its question or
    # corrects its current step, rather than starting a new conversation (see DRAFT_JS).
    '<div id="draft-reply" class="draft-reply" hidden><span>Replying to the draft</span>'
    '<button id="draft-cancel" class="draft-cancel" type="button">Cancel draft</button></div>'
    # Right above the input, where the reader's eyes already are after sending — a status in the
    # header was easy to miss while waiting on a slow answer.
    '<div id="chat-thinking" class="chat-thinking" role="status" aria-live="polite" hidden>'
    '<span class="thinking-dots" aria-hidden="true"><span></span><span></span><span></span></span>'
    '<span id="chat-status"></span></div>'
    '<form id="chat-form" class="chat-form"><div class="chat-composer">'
    '<div class="chat-input-wrap">'
    '<div id="mention-dropdown" class="mention-dropdown"></div>'
    '<input id="chat-input" placeholder="Ask about the docs, or describe a change to draft… '
    '(# to scope)" autocomplete="off">'
    "</div>"
    '<div class="chat-composer-bar">'
    # Auto is the default and the honest one: the server classifies the question. The other two
    # exist for when it reads a question the other way round (see chat_server.classify_intent).
    '<div class="assistant-intent" role="group" aria-label="Answer style">'
    '<button class="chip intent-chip" type="button" data-intent="auto" data-active="true">'
    "Auto</button>"
    '<button class="chip intent-chip" type="button" data-intent="explore" data-active="false">'
    "Explore</button>"
    '<button class="chip intent-chip" type="button" data-intent="spec" data-active="false">'
    "Draft spec</button></div>"
    '<button class="chat-send" type="submit" aria-label="Send">'
    '<svg class="icon" aria-hidden="true"><use href="#icon-send"></use></svg></button>'
    "</div></div></form></div></aside>"
)

_ASSISTANT_TOGGLE = (
    '<button id="chat-toggle" class="chat-toggle" type="button">'
    '<svg class="icon" aria-hidden="true"><use href="#icon-chat"></use></svg>Spec Assistant</button>'
)

_PAGE_TEMPLATE = _env.from_string(
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="color-scheme" content="light dark">'
    "<title>{{ title }}</title>"
    '<link rel="stylesheet" href="assets/site.css"></head>'
    "<body>" + ICON_SPRITE + diagram_render.GLASS_DEFS + '<div class="shell">'
    '<div class="titlebar">'
    '<button id="nav-toggle" class="nav-toggle" type="button" aria-controls="nav-rail" '
    'aria-expanded="true" aria-label="Hide navigation">'
    '<svg class="icon" aria-hidden="true"><use href="#icon-sidebar"></use></svg></button>'
    '<a class="brand" href="index.html">'
    '<svg class="icon" aria-hidden="true"><use href="#icon-brand"></use></svg>specky docs</a>'
    '<div class="search-wrap">'
    '<svg class="icon" aria-hidden="true"><use href="#icon-search"></use></svg>'
    '<input id="search-input" class="search-box" placeholder="Search docs…" '
    'autocomplete="off" aria-label="Search docs">'
    '<div id="search-results"></div>'
    "</div></div>"
    '<div class="body-row">{{ rail | safe }}'
    '<div class="content-pane"><div class="doc"{% if doc_type %} data-type="{{ doc_type }}"'
    '{% endif %}>{{ body | safe }}</div></div>'
    + _ASSISTANT_PANEL
    + "</div></div>"
    + _ASSISTANT_TOGGLE
    # site-data before app: app.js reads SPECKY_INDEX at load. Plain (non-module, non-defer)
    # scripts run in document order, on file:// as well as over http.
    + '<script src="assets/site-data.js"></script>'
    '<script src="assets/app.js"></script>'
    "</body></html>"
)

CSS = """
:root {
  --accent: #0166ff;
  --accent-fg: #ffffff;
  --accent-soft: #e8f1ff;
  --surface: #ffffff;
  --surface-secondary: #f5f6f8;
  --surface-tertiary: #ececf0;
  --border: #e1e3e8;
  --glass-bg: rgb(255 255 255 / 0.7);
  --glass-border: rgb(255 255 255 / 0.6);
  /* The titlebar and sidebar: glass with a wash of the accent in it, so the frame reads as
     color rather than gray. Hover/active are accent alphas, not --surface-tertiary/--accent-soft,
     which both vanish against the tint. */
  --chrome-bg: rgb(234 241 255 / 0.78);
  --chrome-border: rgb(1 102 255 / 0.12);
  --chrome-hover: rgb(1 102 255 / 0.07);
  --chrome-active: rgb(1 102 255 / 0.14);
  --text-primary: #1d1f23;
  --text-secondary: #63666d;
  --text-tertiary: #8b8e96;
  --danger: #d92d20;
  --danger-bg: #fef2f2;
  --feature: #4f46e5;
  --feature-bg: #eef1ff;
  --workflow: #b45309;
  --workflow-bg: #fff7ed;
  --stale: #9f1239;
  --stale-bg: #fdf2f6;
  --tag-0: #0d9488; --tag-0-bg: #e6fbf7;
  --tag-1: #7c3aed; --tag-1-bg: #f2ecff;
  --tag-2: #c2410c; --tag-2-bg: #fef0e7;
  --tag-3: #15803d; --tag-3-bg: #e9fbef;
  --tag-4: #be185d; --tag-4-bg: #fdecf3;
  --tag-5: #4338ca; --tag-5-bg: #ebebfc;
  --tag-6: #a16207; --tag-6-bg: #fbf3df;
  --tag-7: #0e7490; --tag-7-bg: #e5f7fb;
  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --rail-width: 244px;
  --shadow-md: 0 6px 16px -4px rgb(0 0 0 / 0.08), 0 2px 6px -2px rgb(0 0 0 / 0.05);
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  --font-display: Poppins, var(--font-sans);
  --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
}

@media (prefers-color-scheme: dark) {
  :root {
    --accent: #5aa3ff;
    --accent-fg: #0b1220;
    --accent-soft: #16233a;
    --surface: #1c1d21;
    --surface-secondary: #17181b;
    --surface-tertiary: #26282d;
    --border: #34363c;
    --glass-bg: rgb(23 24 27 / 0.6);
    --glass-border: rgb(255 255 255 / 0.08);
    --chrome-bg: rgb(22 30 48 / 0.65);
    --chrome-border: rgb(90 163 255 / 0.14);
    --chrome-hover: rgb(90 163 255 / 0.08);
    --chrome-active: rgb(90 163 255 / 0.16);
    --text-primary: #f0f1f3;
    --text-secondary: #a7aab1;
    --text-tertiary: #75787f;
    --danger: #ff6b6b;
    --danger-bg: #3a1f1f;
    --feature: #96a1ff;
    --feature-bg: #1e2040;
    --workflow: #f0ac5c;
    --workflow-bg: #382a13;
    --stale: #fb7185;
    --stale-bg: #3a1a24;
    --tag-0: #2dd4bf; --tag-0-bg: #0f2b28;
    --tag-1: #a78bfa; --tag-1-bg: #241c3d;
    --tag-2: #fb923c; --tag-2-bg: #3a2413;
    --tag-3: #4ade80; --tag-3-bg: #163521;
    --tag-4: #f472b6; --tag-4-bg: #3a1a2b;
    --tag-5: #818cf8; --tag-5-bg: #1f2145;
    --tag-6: #fbbf24; --tag-6-bg: #3a2f0f;
    --tag-7: #22d3ee; --tag-7-bg: #0f2c33;
    --shadow-md: 0 6px 20px -4px rgb(0 0 0 / 0.5), 0 2px 8px -2px rgb(0 0 0 / 0.35);
  }
}

* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  font-family: var(--font-sans);
  font-size: 0.8125rem;
  line-height: 1.5;
  color: var(--text-primary);
  background: var(--surface);
}
a { color: inherit; }
.icon {
  width: 1em; height: 1em; flex-shrink: 0; fill: none; stroke: currentColor;
  stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; vertical-align: -0.15em;
}
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }

.shell { height: 100vh; }

/* --- glass: the titlebar floats fixed above the sidebar/content scroll regions (both
   start at y=0 and pad their content below it, see .sidebar/.doc), so it's genuinely
   blurring scrolled content behind it, not just tinting flat background — per the "blur on
   the floating layer, never in content" rule the rest of the chrome follows too. Diagrams are
   the one deliberate exception, and they only imitate glass (a translucent sheen, no blur —
   see diagram_render.py). Falls back to a plain solid bar on engines without backdrop-filter. */
.titlebar {
  position: fixed; top: 0; left: 0; right: 0; z-index: 30;
  display: flex; align-items: center; gap: 20px; height: 52px; padding: 0 18px;
  background: var(--chrome-bg); backdrop-filter: blur(20px) saturate(160%);
  -webkit-backdrop-filter: blur(20px) saturate(160%); border-bottom: 1px solid var(--chrome-border);
}
.titlebar .brand {
  display: flex; align-items: center; gap: 8px; font-family: var(--font-display);
  font-weight: 600; font-size: 0.9375rem; letter-spacing: -0.01em; color: var(--text-primary);
  text-decoration: none; flex-shrink: 0;
}
.titlebar .brand .icon { width: 1.2em; height: 1.2em; color: var(--accent); }
.nav-toggle {
  display: inline-flex; align-items: center; border: none; background: none; color: var(--text-secondary);
  padding: 5px; margin-right: -10px; border-radius: var(--radius-sm); cursor: pointer; flex-shrink: 0;
}
.nav-toggle .icon { width: 1.1em; height: 1.1em; }
.nav-toggle:hover { background: var(--chrome-hover); color: var(--text-primary); }
.search-wrap { position: relative; flex: 1; max-width: 380px; }
.search-wrap > .icon {
  position: absolute; left: 9px; top: 50%; transform: translateY(-50%); color: var(--text-tertiary);
}
.search-box {
  width: 100%; padding: 6px 10px 6px 30px; border: 1px solid var(--border); border-radius: var(--radius-md);
  font-family: var(--font-sans); font-size: 0.75rem; background: var(--surface); color: var(--text-primary);
}
#search-results {
  position: absolute; top: calc(100% + 6px); left: 0; right: 0; z-index: 25;
  background: var(--glass-bg); backdrop-filter: blur(20px) saturate(160%);
  -webkit-backdrop-filter: blur(20px) saturate(160%); border: 1px solid var(--glass-border);
  border-radius: var(--radius-md); box-shadow: var(--shadow-md); overflow: hidden;
}
#search-results:empty { display: none; border: none; box-shadow: none; }
#search-results .hit { display: block; padding: 8px 12px; text-decoration: none; color: var(--text-primary); }
#search-results .hit:hover, #search-results .hit:focus-visible { background: var(--surface-tertiary); }
#search-results .hit .icon { margin-right: 6px; }
#search-results .hit-domain { color: var(--text-tertiary); font-size: 0.6875rem; }
#search-results .hit-context {
  color: var(--text-secondary); font-size: 0.6875rem; margin-top: 3px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
#search-results .hit-context mark {
  background: var(--accent-soft); color: var(--text-primary); border-radius: 2px; padding: 0 1px;
}
#search-results { max-height: 60vh; overflow-y: auto; }

.body-row { display: flex; height: 100vh; }
.sidebar {
  width: var(--rail-width); flex-shrink: 0; background: var(--chrome-bg); backdrop-filter: blur(20px) saturate(160%);
  -webkit-backdrop-filter: blur(20px) saturate(160%); border-right: 1px solid var(--chrome-border);
  padding: 66px 14px 20px; overflow-y: auto; height: 100vh; box-sizing: border-box;
}
/* Collapsed from the titlebar, and while the Spec Assistant is open (see NAV_JS/CHAT_JS): the
   rail's width is worth more to an answer than to a nav the reader isn't using mid-conversation.
   Zeroing --rail-width tells the panel's max-width the room is free. */
body.nav-collapsed { --rail-width: 0px; }
body.nav-collapsed .sidebar { display: none; }
.domain-group { margin-bottom: 18px; }
.domain-group summary {
  display: flex; align-items: center; gap: 6px; font-size: 0.6875rem; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--text-secondary); font-weight: 600; margin: 0 0 8px;
  cursor: pointer; user-select: none;
}
.domain-group summary::marker { color: var(--text-tertiary); font-size: 0.7em; }
.domain-group summary .icon { color: var(--text-tertiary); }
.domain-group ul { list-style: none; margin: 0; padding: 0; }
.domain-group li { margin-bottom: 2px; }
.domain-group a {
  display: flex; align-items: center; gap: 8px; padding: 6px 10px; border-radius: var(--radius-md);
  color: var(--text-primary); text-decoration: none; font-size: 0.8125rem;
}
.domain-group a:hover { background: var(--chrome-hover); }
.domain-group a.active { background: var(--chrome-active); color: var(--accent); font-weight: 600; }
/* Feature vs workflow at a glance: the same icon and color as the doc's type chip. */
.domain-group a .icon, #search-results .hit .icon { color: var(--text-tertiary); }
.domain-group a[data-type="feature"] .icon, #search-results .hit[data-type="feature"] .icon {
  color: var(--feature);
}
.domain-group a[data-type="workflow"] .icon, #search-results .hit[data-type="workflow"] .icon {
  color: var(--workflow);
}

.tag-filter { margin-top: 8px; padding-top: 14px; border-top: 1px solid var(--border); }
.filter-title {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;
  font-size: 0.6875rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-secondary);
  font-weight: 600;
}
.clear-filter {
  background: none; border: none; color: var(--accent); font-size: 0.6875rem; font-family: var(--font-sans);
  cursor: pointer; padding: 0; font-weight: 600;
}
.chip-row { display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  display: inline-flex; align-items: center; gap: 4px; border: none; border-radius: 999px;
  padding: 4px 10px; font-size: 0.6875rem; font-weight: 600; cursor: pointer; font-family: var(--font-sans);
  background: var(--surface-tertiary); color: var(--text-secondary);
}
.chip .icon { width: 0.9em; height: 0.9em; }
.chip[data-active="true"] { box-shadow: 0 0 0 2px currentColor inset; }
.chip.tag-0 { background: var(--tag-0-bg); color: var(--tag-0); }
.chip.tag-1 { background: var(--tag-1-bg); color: var(--tag-1); }
.chip.tag-2 { background: var(--tag-2-bg); color: var(--tag-2); }
.chip.tag-3 { background: var(--tag-3-bg); color: var(--tag-3); }
.chip.tag-4 { background: var(--tag-4-bg); color: var(--tag-4); }
.chip.tag-5 { background: var(--tag-5-bg); color: var(--tag-5); }
.chip.tag-6 { background: var(--tag-6-bg); color: var(--tag-6); }
.chip.tag-7 { background: var(--tag-7-bg); color: var(--tag-7); }
.chip.type-feature { background: var(--feature-bg); color: var(--feature); }
.chip.type-workflow { background: var(--workflow-bg); color: var(--workflow); }
.chip.stale { background: var(--stale-bg); color: var(--stale); }

.content-pane {
  flex: 1; height: 100vh; overflow-y: auto; display: flex; justify-content: center; background: var(--surface);
}
.doc { width: 100%; max-width: 1040px; padding: 66px 48px 72px; }
/* A doc's body accents follow its type, so a feature reads indigo and a workflow amber
   everywhere it appears (chip, sidebar icon, page); anything unclassified takes the accent. */
.doc { --doc-tint: var(--accent); --doc-tint-bg: var(--accent-soft); }
.doc[data-type="feature"] { --doc-tint: var(--feature); --doc-tint-bg: var(--feature-bg); }
.doc[data-type="workflow"] { --doc-tint: var(--workflow); --doc-tint-bg: var(--workflow-bg); }
.breadcrumb {
  display: flex; align-items: center; gap: 6px; font-size: 0.6875rem; color: var(--text-secondary);
  text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 8px;
}
.breadcrumb .icon { color: var(--text-tertiary); }
.doc-tags { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 20px; }
.doc-owner {
  display: flex; align-items: center; gap: 6px; font-size: 0.8125rem; color: var(--text-secondary);
  margin: -8px 0 20px;
}
.doc-owner .icon { color: var(--text-tertiary); }
.doc-owner strong { color: var(--text-primary); font-weight: 500; }
.doc h1 { font-family: var(--font-display); font-size: 1.625rem; letter-spacing: -0.03em; margin-top: 0; }
.doc h2 {
  font-size: 1.125rem; letter-spacing: -0.01em; margin-top: 32px; border-top: 1px solid var(--border);
  padding-top: 20px;
}
.doc h2::before {
  content: ""; display: inline-block; width: 4px; height: 0.95em; margin-right: 10px;
  border-radius: 2px; background: var(--doc-tint); vertical-align: -0.12em;
}
.doc h3 { font-size: 1rem; }
.doc a { color: var(--accent); text-decoration: underline; text-decoration-color: var(--accent-soft); }
.doc code {
  font-family: var(--font-mono); background: var(--surface-tertiary); padding: 2px 5px; border-radius: 4px;
  font-size: 0.75rem;
}
.doc pre {
  background: var(--text-primary); color: var(--surface); padding: 16px; border-radius: var(--radius-md);
  overflow-x: auto;
}
.doc pre code { background: none; color: inherit; padding: 0; }
.doc blockquote {
  border-left: 3px solid var(--doc-tint); background: var(--doc-tint-bg); margin: 0; padding: 4px 16px;
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0; color: var(--text-secondary);
}

.related { margin-top: 40px; padding-top: 20px; border-top: 1px solid var(--border); }
.related h2 {
  font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-secondary);
  margin: 0 0 10px; border: none; padding: 0;
}
.related h2::before, .tag-cloud h2::before { content: none; }
.related ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.related a { display: inline-flex; align-items: center; gap: 6px; text-decoration: none; }
.related a:hover { text-decoration: underline; }

.stat-row { display: flex; gap: 12px; margin: 20px 0 0; flex-wrap: wrap; }
.stat {
  background: var(--surface-secondary); border: 1px solid var(--border); border-radius: var(--radius-md);
  padding: 12px 16px; min-width: 96px;
}
.stat .n { font-size: 1.375rem; font-weight: 600; color: var(--accent); display: block; }
.stat .label { font-size: 0.6875rem; color: var(--text-secondary); }
.tag-cloud { margin-top: 28px; }
.tag-cloud h2 {
  font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-secondary);
  margin: 0 0 10px; border: none; padding: 0;
}
.empty-state { color: var(--text-secondary); }

/* --- tables: ported from glia's figure.tw (enrichment_render.py) — the wrapper scrolls,
   so a wide table keeps its own column sizing instead of being squeezed into the pane. */
.doc figure.tw { margin: 16px 0; overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius-md); }
.doc figure.tw table { border-collapse: collapse; width: 100%; margin: 0; font-size: 0.8125rem; }
.doc figure.tw th, .doc figure.tw td {
  padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top;
}
.doc figure.tw thead th {
  background: var(--doc-tint-bg); color: var(--doc-tint); font-weight: 600;
  font-size: 0.6875rem; text-transform: uppercase; letter-spacing: 0.05em;
}
.doc figure.tw tbody tr:nth-child(even) { background: var(--surface-secondary); }
.doc figure.tw tbody tr:last-child td { border-bottom: none; }

/* --- workflow steps: the happy path under `## How It Works`, numbered by a counter rather
   than by <ol>'s own marker so the number can sit in its own chip on the rail. Only rendered
   for doc_type == workflow (see `_step_list`); every other list keeps the plain <ol>. */
.doc ol.steps { counter-reset: step; list-style: none; margin: 20px 0; padding: 0; }
.doc ol.steps li {
  counter-increment: step; position: relative; margin: 0; padding: 0 0 18px 42px;
}
.doc ol.steps li::before {
  content: counter(step);
  position: absolute; left: 0; top: 0;
  width: 26px; height: 26px; box-sizing: border-box;
  display: flex; align-items: center; justify-content: center;
  border: 1px solid var(--workflow-bg); border-radius: 50%;
  background: var(--workflow-bg); color: var(--workflow);
  font-size: 0.75rem; font-weight: 600; font-variant-numeric: tabular-nums;
}
/* The rail joining one step to the next — not drawn past the last one. */
.doc ol.steps li::after {
  content: ""; position: absolute; left: 13px; top: 30px; bottom: 4px;
  border-left: 1px solid var(--border);
}
.doc ol.steps li:last-child { padding-bottom: 0; }
.doc ol.steps li:last-child::after { display: none; }
.doc ol.steps .st { display: block; color: var(--text-primary); font-weight: 600; line-height: 26px; }
.doc ol.steps .sd { display: block; color: var(--text-secondary); margin-top: 2px; }

/* --- glossary hover terms: ported from glia's `.gl`/`.tip` (enrichment_render.py) --- */
.doc .gl { border-bottom: 1px dotted var(--accent); cursor: help; }
.tip {
  position: absolute; z-index: 40; max-width: 34ch;
  background: var(--text-primary); color: var(--surface);
  padding: 8px 10px; border-radius: var(--radius-md); font-size: 0.75rem; line-height: 1.45;
  box-shadow: var(--shadow-md); pointer-events: none;
}
.tip[hidden] { display: none; }

.chat-toggle {
  position: fixed; bottom: 24px; right: 24px; z-index: 20; display: inline-flex; align-items: center; gap: 6px;
  background: var(--accent); color: var(--accent-fg); border: none; border-radius: var(--radius-lg);
  padding: 10px 18px; font-family: var(--font-sans); font-size: 0.8125rem; font-weight: 600;
  box-shadow: var(--shadow-md); cursor: pointer;
}
/* --- the Spec Assistant dock: a column of .body-row, so opening it reflows the content pane instead of
   covering it. Width is a custom property the drag handle writes (see CHAT_JS), and the
   titlebar clearance mirrors .sidebar's — both scroll under the fixed bar. */
.assistant-panel {
  /* min-width: 0 — a flex item's automatic minimum is its content, and one wide diagram or a long
     code line would otherwise push the dock past the width the reader dragged it to. */
  position: relative; flex: 0 0 var(--assistant-width, 560px); min-width: 0; height: 100vh; z-index: 20;
  /* A width dragged wide still leaves the doc column 360px to be read in — after the window shrinks,
     or the reader brings back the rail. */
  max-width: calc(100vw - 360px - var(--rail-width));
  background: var(--glass-bg); backdrop-filter: blur(24px) saturate(180%);
  -webkit-backdrop-filter: blur(24px) saturate(180%); border-left: 1px solid var(--glass-border);
  display: none;
}
body.assistant-open .assistant-panel { display: block; }
body.assistant-open .chat-toggle { display: none; }
.assistant-body { display: flex; flex-direction: column; height: 100%; padding-top: 52px; }
.assistant-resize {
  position: absolute; top: 0; bottom: 0; left: -3px; width: 7px; z-index: 2; cursor: col-resize;
}
.assistant-resize:hover, .assistant-resize:focus-visible { background: var(--accent-soft); }
.assistant-close {
  border: none; background: none; color: var(--text-secondary); font-size: 1.125rem; line-height: 1;
  padding: 0 2px; cursor: pointer;
}
.assistant-close:hover { color: var(--text-primary); }
.assistant-intent { display: flex; flex-wrap: wrap; gap: 6px; }
.intent-chip[data-active="true"] { background: var(--accent-soft); color: var(--accent); }
.chat-header {
  padding: 12px 16px; font-weight: 600; font-size: 0.8125rem; border-bottom: 1px solid var(--border);
  display: flex; justify-content: space-between; align-items: center; gap: 8px;
}
.chat-title { display: flex; align-items: center; gap: 6px; }
.chat-title .icon { color: var(--accent); }
/* Narrow windows have no width to give: the dock overlays the content instead of crushing the
   doc column to an unreadable ribbon. */
@media (max-width: 1100px) {
  .assistant-panel {
    position: fixed; top: 0; right: 0; bottom: 0; z-index: 26; flex: none; max-width: none;
    width: min(var(--assistant-width, 560px), 100vw); box-shadow: var(--shadow-md);
  }
}
.chat-header-right { display: flex; align-items: center; gap: 8px; }
.chat-reset {
  border: 1px solid var(--border); background: none; color: var(--text-secondary);
  font: inherit; font-weight: 400; font-size: 0.6875rem; padding: 2px 8px;
  border-radius: var(--radius-sm); cursor: pointer;
}
.chat-reset:hover { color: var(--text-primary); border-color: var(--text-tertiary); }
.chat-log { flex: 1; overflow-y: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 8px; min-height: 120px; min-width: 0; }
.chat-msg { font-size: 0.75rem; line-height: 1.5; padding: 6px 10px; border-radius: var(--radius-md); max-width: 90%; min-width: 0; white-space: pre-wrap; }
.chat-user { align-self: flex-end; background: var(--accent-soft); color: var(--text-primary); }
.chat-assistant { align-self: flex-start; background: var(--surface-tertiary); color: var(--text-primary); }
.chat-sources { align-self: flex-start; color: var(--text-secondary); font-size: 0.6875rem; }
.chat-error { align-self: flex-start; color: var(--danger); background: var(--danger-bg); }
/* --- the composer: the input with its options right under it, so what the answer will be
   (Auto / Explore / Draft spec) is decided where the question is typed, not at the top of the panel. */
.chat-form { padding: 8px 16px 14px; }
.chat-composer {
  display: flex; flex-direction: column; gap: 6px; padding: 6px 8px 8px;
  border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface);
}
.chat-composer:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.chat-input-wrap { position: relative; }
/* The composer shows focus for the input, so the input itself draws no box or outline. */
.chat-form input {
  width: 100%; padding: 6px 4px; border: none; outline: none; background: transparent;
  font-family: var(--font-sans); font-size: 0.8125rem; color: var(--text-primary); box-sizing: border-box;
}
.chat-composer-bar { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.chat-send {
  background: var(--accent); color: var(--accent-fg); border: none; border-radius: var(--radius-md);
  padding: 7px 12px; display: inline-flex; align-items: center; cursor: pointer; flex-shrink: 0;
}
/* --- waiting on the server: a line directly above the composer, animated so it reads as
   "working" at a glance. DRAFT_STATUS names the draft step in the same spot. */
.chat-thinking {
  display: flex; align-items: center; gap: 8px; padding: 8px 20px 0;
  font-size: 0.75rem; font-weight: 600; color: var(--accent);
}
.chat-thinking[hidden] { display: none; }
.thinking-dots { display: inline-flex; gap: 3px; }
.thinking-dots span {
  width: 6px; height: 6px; border-radius: 50%; background: currentColor;
  animation: thinking-pulse 1.2s ease-in-out infinite;
}
.thinking-dots span:nth-child(2) { animation-delay: 0.15s; }
.thinking-dots span:nth-child(3) { animation-delay: 0.3s; }
@keyframes thinking-pulse {
  0%, 80%, 100% { opacity: 0.25; transform: scale(0.8); }
  40% { opacity: 1; transform: scale(1); }
}
@media (prefers-reduced-motion: reduce) {
  .thinking-dots span { animation: none; opacity: 0.6; }
}
.mention-dropdown {
  position: absolute; bottom: calc(100% + 6px); left: 0; right: 0; z-index: 25;
  background: var(--glass-bg); backdrop-filter: blur(20px) saturate(160%);
  -webkit-backdrop-filter: blur(20px) saturate(160%); border: 1px solid var(--glass-border);
  border-radius: var(--radius-md); box-shadow: var(--shadow-md); overflow: hidden;
  max-height: 180px; overflow-y: auto;
}
.mention-dropdown:empty { display: none; border: none; box-shadow: none; }
.mention-dropdown .mention-item {
  display: flex; width: 100%; justify-content: space-between; align-items: center; gap: 8px;
  padding: 6px 10px; border: none; background: none; text-align: left; cursor: pointer;
  font-family: var(--font-sans); font-size: 0.75rem; color: var(--text-primary);
}
.mention-dropdown .mention-item:hover, .mention-dropdown .mention-item:focus-visible {
  background: var(--surface-tertiary);
}
.mention-dropdown .mention-item .kind { color: var(--text-tertiary); font-size: 0.6875rem; flex-shrink: 0; }
/* --- a rendered answer: the panel's version of `.doc`, not a chat bubble. Full width of the
   log (a table or diagram has nowhere to go in a 90% bubble), normal wrapping (the markdown is
   real HTML now, not preformatted text), and the doc page's own figure/table/diagram styling
   reused as-is — the same server-side pipeline produced both. */
.chat-rich {
  align-self: stretch; max-width: 100%; white-space: normal; background: none; padding: 2px 0;
  font-size: 0.8125rem;
}
.chat-rich > :first-child { margin-top: 0; }
.chat-rich > :last-child { margin-bottom: 0; }
.chat-rich h1, .chat-rich h2, .chat-rich h3, .chat-rich h4 {
  font-family: var(--font-display); letter-spacing: -0.01em; margin: 16px 0 6px; border: none;
  padding: 0;
}
.chat-rich h1 { font-size: 1rem; }
.chat-rich h2 { font-size: 0.9375rem; }
.chat-rich h3, .chat-rich h4 { font-size: 0.875rem; }
.chat-rich p, .chat-rich ul, .chat-rich ol { margin: 8px 0; }
.chat-rich ul, .chat-rich ol { padding-left: 20px; }
.chat-rich li { margin-bottom: 3px; }
.chat-rich a { color: var(--accent); }
.chat-rich code {
  font-family: var(--font-mono); background: var(--surface-tertiary); padding: 1px 4px;
  border-radius: 4px; font-size: 0.75rem;
}
.chat-rich pre {
  background: var(--text-primary); color: var(--surface); padding: 12px; border-radius: var(--radius-md);
  overflow-x: auto; font-size: 0.75rem;
}
.chat-rich pre code { background: none; color: inherit; padding: 0; }
.chat-rich blockquote {
  border-left: 3px solid var(--accent-soft); margin: 8px 0; padding: 2px 12px; color: var(--text-secondary);
}
.chat-rich figure.tw { margin: 10px 0; overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius-md); }
.chat-rich figure.tw table { border-collapse: collapse; width: 100%; font-size: 0.75rem; }
.chat-rich figure.tw th, .chat-rich figure.tw td {
  padding: 6px 8px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top;
}
.chat-rich figure.tw thead th {
  background: var(--surface-secondary); color: var(--text-secondary); font-weight: 600;
  font-size: 0.625rem; text-transform: uppercase; letter-spacing: 0.05em;
}
.chat-rich figure.tw tbody tr:nth-child(even) { background: var(--surface-secondary); }
.chat-rich .gl { border-bottom: 1px dotted var(--accent); cursor: help; }

.chat-actions {
  align-self: stretch; display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
  font-size: 0.6875rem; color: var(--text-secondary);
}
.chat-actions .intent-badge {
  border-radius: 999px; padding: 2px 8px; font-weight: 600; background: var(--surface-tertiary);
  color: var(--text-secondary);
}
.chat-actions .intent-badge[data-intent="spec"] { background: var(--feature-bg); color: var(--feature); }
.chat-copy {
  border: 1px solid var(--border); background: none; color: var(--text-secondary); font: inherit;
  padding: 2px 8px; border-radius: var(--radius-sm); cursor: pointer;
}
.chat-copy:hover { color: var(--text-primary); border-color: var(--text-tertiary); }
.chat-source-link { color: var(--accent); text-decoration: none; }
.chat-source-link:hover { text-decoration: underline; }
.chat-note { align-self: stretch; color: var(--text-tertiary); font-size: 0.6875rem; font-style: italic; }
/* --- Explore: the short answer shows, the details wait behind "Read more". */
.chat-details[hidden] { display: none; }
.chat-details { border-top: 1px dashed var(--border); margin-top: 8px; padding-top: 4px; }
.chat-more {
  align-self: flex-start; border: none; background: none; color: var(--accent); font: inherit;
  font-size: 0.75rem; font-weight: 600; padding: 0; cursor: pointer;
}
.chat-more:hover { text-decoration: underline; }
/* --- a draft step: built from structured data in the browser (DRAFT_JS), styled like an answer. */
.draft-card { border: 1px solid var(--border); border-radius: var(--radius-md); padding: 10px 12px; }
.draft-steps { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 8px; font-size: 0.625rem; }
.draft-step {
  border-radius: 999px; padding: 2px 8px; background: var(--surface-tertiary);
  color: var(--text-tertiary); font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em;
}
.draft-step[data-state="done"] { color: var(--text-secondary); }
.draft-step[data-state="active"] { background: var(--feature-bg); color: var(--feature); }
.draft-target { font-size: 0.75rem; color: var(--text-secondary); margin: 0 0 6px; }
.draft-target code { font-size: 0.6875rem; }
.draft-target .draft-kind {
  border-radius: 999px; padding: 1px 6px; margin-left: 4px; font-size: 0.625rem; font-weight: 600;
  background: var(--accent-soft); color: var(--accent);
}
.chat-rich .draft-card-title { margin: 10px 0 4px; font-size: 0.8125rem; }
.draft-options, .draft-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.draft-action {
  border: 1px solid var(--border); background: none; color: var(--text-primary); font: inherit;
  font-size: 0.75rem; padding: 4px 10px; border-radius: var(--radius-sm); cursor: pointer;
}
.draft-action.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-fg); }
.draft-action:disabled { opacity: 0.45; cursor: default; }
.draft-hint { color: var(--text-tertiary); font-size: 0.6875rem; margin: 6px 0 0; }
.draft-warning {
  color: var(--danger); background: var(--danger-bg); border-radius: var(--radius-sm);
  padding: 6px 8px; font-size: 0.75rem; margin: 6px 0;
}
.draft-diff[hidden] { display: none; }
.draft-diff { white-space: pre; max-height: 320px; overflow: auto; }
.draft-kind-changed, .draft-kind-modify { color: var(--feature); }
.draft-kind-new, .draft-kind-add { color: var(--accent); }
.draft-kind-removed, .draft-kind-remove { color: var(--danger); }
.draft-reply {
  display: flex; justify-content: space-between; align-items: center; gap: 8px;
  padding: 6px 16px; border-top: 1px solid var(--border); font-size: 0.6875rem;
  color: var(--feature); background: var(--feature-bg);
}
.draft-reply[hidden] { display: none; }
.draft-cancel {
  border: none; background: none; color: var(--text-secondary); font: inherit; cursor: pointer;
  text-decoration: underline;
}
"""

# Finding the companion server from wherever this page was opened. `specky serve` serves this page
# *and* the API, so same-origin is the right first guess for any http(s) page — including one on
# `serve --port N`, a port SPECKY_CHAT_PORT (baked in at render time) can't know about. If that
# origin turns out to be a plain web server that doesn't know these routes, one 404/405 switches to
# the chat port on the same host, and that choice sticks for the rest of the page's life. A file://
# page has no origin to hang a relative URL off, so it starts at loopback.
API_JS = """
const SPECKY_SERVED = location.protocol.startsWith('http');
const SPECKY_API_FALLBACK = SPECKY_SERVED
  ? `${location.protocol}//${location.hostname}:${SPECKY_CHAT_PORT}`
  : `http://127.0.0.1:${SPECKY_CHAT_PORT}`;
// Statuses a static file server gives an API route it has never heard of.
const SPECKY_NOT_THE_API = new Set([404, 405, 501]);
let speckyApiBase = SPECKY_SERVED ? '' : SPECKY_API_FALLBACK;
let speckyApiSettled = !SPECKY_SERVED;

async function speckyFetch(path, init) {
  try {
    const res = await fetch(`${speckyApiBase}${path}`, init);
    if (speckyApiSettled || !SPECKY_NOT_THE_API.has(res.status)) {
      speckyApiSettled = true;
      return res;
    }
  } catch (err) {
    if (speckyApiSettled) throw err;
  }
  speckyApiBase = SPECKY_API_FALLBACK;
  speckyApiSettled = true;
  return fetch(`${speckyApiBase}${path}`, init);
}
"""

# Search runs twice per keystroke, on purpose. The in-page index answers instantly and works with
# no server (`d.body` is the whole doc up to the budget in `search_body_cap`, or absent on a repo
# too large for that). Then, if a `specky serve` is reachable, the same query goes to its /search
# endpoint, which matches the *complete* FTS5 index and replaces the local guess. Offline is
# best-effort; served is exact.
SEARCH_JS = (
    f"const TYPE_ICONS = {json.dumps(_TYPE_ICONS)};\n"
    f"const TYPE_ICON_FALLBACK = {json.dumps(_TYPE_ICON_FALLBACK)};\n"
    + """
const searchInput = document.getElementById('search-input');
const searchResults = document.getElementById('search-results');
const docsByPath = new Map(SPECKY_INDEX.map((d) => [d.path, d]));
const SEARCH_CONTEXT_CHARS = 60;
const SEARCH_HIT_LIMIT = 15;
let searchSeq = 0;

function docMatchesFilters(tags, docType, stale) {
  const tagOk = activeTags.size === 0 || tags.some((t) => activeTags.has(t));
  const typeOk = !activeType || docType === activeType;
  return tagOk && typeOk && (!activeStale || !!stale);
}

function escapeHtml(text) {
  return text.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// ±60 chars around the first match, so a hit deep inside a long doc shows *why* it matched.
function contextAround(body, q) {
  const at = body.indexOf(q);
  if (at < 0) return '';
  const start = Math.max(0, at - SEARCH_CONTEXT_CHARS);
  const end = Math.min(body.length, at + q.length + SEARCH_CONTEXT_CHARS);
  return (start > 0 ? '…' : '')
    + escapeHtml(body.slice(start, at))
    + `<mark>${escapeHtml(body.slice(at, at + q.length))}</mark>`
    + escapeHtml(body.slice(at + q.length, end))
    + (end < body.length ? '…' : '');
}

// FTS5 snippets arrive with the '>>'/'<<' markers indexer.search asks for; escape first, then
// promote the (now escaped) markers to <mark> — never the other way round.
function markSnippet(snippet) {
  return escapeHtml(snippet).replaceAll('&gt;&gt;', '<mark>').replaceAll('&lt;&lt;', '</mark>');
}

function showHits(hits) {
  searchResults.innerHTML = '';
  for (const hit of hits) {
    const a = document.createElement('a');
    a.className = 'hit';
    a.href = hit.doc.html_path;
    a.dataset.type = hit.doc.doc_type || '';
    const icon = TYPE_ICONS[hit.doc.doc_type] || TYPE_ICON_FALLBACK;
    a.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#icon-${icon}"></use></svg>`
      + `${escapeHtml(hit.doc.title)}<div class="hit-domain">${escapeHtml(hit.doc.domain)}</div>`
      + (hit.context ? `<div class="hit-context">${hit.context}</div>` : '');
    searchResults.appendChild(a);
  }
}

function localHits(q) {
  const hits = [];
  for (const d of SPECKY_INDEX) {
    if (!docMatchesFilters(d.tags, d.doc_type, d.stale)) continue;
    const inBody = d.body ? d.body.includes(q) : false;
    if (!inBody && !d.title.toLowerCase().includes(q) && !d.domain.toLowerCase().includes(q)
        && !d.excerpt.toLowerCase().includes(q)) continue;
    hits.push({ doc: d, context: inBody ? contextAround(d.body, q) : '' });
    if (hits.length === SEARCH_HIT_LIMIT) break;
  }
  return hits;
}

// Tried from file:// too, not just when served: if `specky serve` is running, its index is the
// better answer, and if it isn't, the fetch simply fails and the local hits already on screen stay.
async function servedHits(q) {
  const res = await speckyFetch(`/search?q=${encodeURIComponent(q)}&limit=${SEARCH_HIT_LIMIT}`);
  if (!res.ok) return null;
  const data = await res.json();
  const hits = [];
  for (const row of data.results || []) {
    const doc = docsByPath.get(row.path);
    if (!doc || !docMatchesFilters(doc.tags, doc.doc_type, doc.stale)) continue;
    hits.push({ doc, context: markSnippet(row.snippet || '') });
  }
  return hits;
}

searchInput?.addEventListener('input', () => {
  const q = searchInput.value.trim().toLowerCase();
  const seq = ++searchSeq;
  if (!q) {
    searchResults.innerHTML = '';
    return;
  }
  showHits(localHits(q));
  servedHits(q)
    .then((hits) => {
      // Ignore a response the user has already typed past, and an empty served result set that
      // would blank out usable local hits.
      if (hits && hits.length && seq === searchSeq) showHits(hits);
    })
    .catch(() => { /* no server reachable — the local hits stand */ });
});
"""
)

# Every colored chip (sidebar filter row, doc-header tags/type badge, home tag cloud) is a
# <button data-facet="tag|type">; clicking any of them toggles the same filter state and
# re-applies it to whichever sidebar/search results are on the current page — the chips
# don't navigate, so a doc page's own tags can filter that page's own sidebar nav.
FILTER_JS = """
const activeTags = new Set();
let activeType = null;
// Boolean rather than a value facet: "stale" is a property of a doc, not one option among many.
let activeStale = false;
const filterChips = document.querySelectorAll('.chip[data-facet]');
const navLinks = document.querySelectorAll('.sidebar .domain-group a');
const clearFilterBtn = document.getElementById('clear-filter');

function applyFilters() {
  const anyActive = activeTags.size > 0 || activeType || activeStale;
  navLinks.forEach((a) => {
    const tags = (a.dataset.tags || '').split(',').filter(Boolean);
    const docType = a.dataset.type || '';
    const stale = a.dataset.stale === 'true';
    a.closest('li').style.display = docMatchesFilters(tags, docType, stale) ? '' : 'none';
  });
  document.querySelectorAll('.domain-group').forEach((group) => {
    const anyVisible = [...group.querySelectorAll('li')].some((li) => li.style.display !== 'none');
    group.style.display = anyVisible ? '' : 'none';
    if (anyActive && anyVisible) group.open = true;
  });
  filterChips.forEach((chip) => {
    let isActive;
    if (chip.dataset.facet === 'tag') isActive = activeTags.has(chip.dataset.tag);
    else if (chip.dataset.facet === 'stale') isActive = activeStale;
    else isActive = chip.dataset.type === activeType;
    chip.dataset.active = isActive ? 'true' : 'false';
  });
  if (clearFilterBtn) clearFilterBtn.hidden = !anyActive;
  if (searchInput && searchInput.value.trim()) searchInput.dispatchEvent(new Event('input'));
}

filterChips.forEach((chip) => {
  chip.addEventListener('click', () => {
    if (chip.dataset.facet === 'tag') {
      const t = chip.dataset.tag;
      activeTags.has(t) ? activeTags.delete(t) : activeTags.add(t);
    } else if (chip.dataset.facet === 'stale') {
      activeStale = !activeStale;
    } else {
      activeType = activeType === chip.dataset.type ? null : chip.dataset.type;
    }
    applyFilters();
  });
});

clearFilterBtn?.addEventListener('click', () => {
  activeTags.clear();
  activeType = null;
  activeStale = false;
  applyFilters();
});
"""

# The sidebar is byte-identical on every page, so it's rendered once and the "you are here"
# state is derived here instead of being baked into each page's HTML. Compares href values
# rather than building a selector out of the filename — no escaping question to get wrong.
NAV_JS = """
const currentPage = decodeURIComponent(location.pathname.split('/').pop()) || 'index.html';
const currentLink = [...document.querySelectorAll('.sidebar .domain-group a')]
  .find((a) => a.getAttribute('href') === currentPage);
if (currentLink) {
  currentLink.classList.add('active');
  currentLink.closest('details')?.setAttribute('open', '');
}

// The tab's own state — the rail here, the conversation and panel in CHAT_JS — lives in
// sessionStorage. A file:// page may refuse storage outright, and a sandboxed iframe always does.
// Failing that probe costs the reader continuity, not function: the rail and panel start from their
// defaults on every page, and a follow-up asked after navigating starts a fresh conversation.
const tabStore = (() => {
  try {
    window.sessionStorage.getItem('specky-chat-session');
    return window.sessionStorage;
  } catch (err) {
    return null;
  }
})();

// --- collapsing the rail: remembered for the tab, like the assistant panel's own state.
const navToggle = document.getElementById('nav-toggle');
const navRail = document.getElementById('nav-rail');

function isNavCollapsed() {
  return document.body.classList.contains('nav-collapsed');
}

function setNavCollapsed(collapsed) {
  document.body.classList.toggle('nav-collapsed', collapsed);
  tabStore?.setItem('specky-nav-collapsed', collapsed ? '1' : '0');
  navToggle?.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  navToggle?.setAttribute('aria-label', collapsed ? 'Show navigation' : 'Hide navigation');
}

navToggle?.addEventListener('click', () => setNavCollapsed(!isNavCollapsed()));
setNavCollapsed(tabStore?.getItem('specky-nav-collapsed') === '1');
"""

# The viewer is many pages, and the reader navigates between them mid-conversation, so both halves
# of a conversation have to outlive the page: the session id the server keys its transcript on, and
# the transcript the panel shows. sessionStorage holds both — the tab, not the browser, is the right
# lifetime for "the conversation I'm having now", and it's the reader's own tab either way. The
# panel's own state (open, width, pinned intent) lives there too, for the same reason: a reader who
# clicks a cited source shouldn't find the panel gone and 560px back to its default.
CHAT_JS = """
const chatToggle = document.getElementById('chat-toggle');
const assistantPanel = document.getElementById('assistant-panel');
const assistantClose = document.getElementById('assistant-close');
const assistantResize = document.getElementById('assistant-resize');
const chatLog = document.getElementById('chat-log');
const chatForm = document.getElementById('chat-form');
const chatInput = document.getElementById('chat-input');
const chatStatus = document.getElementById('chat-status');
const chatThinking = document.getElementById('chat-thinking');
const chatReset = document.getElementById('chat-reset');
const intentChips = [...document.querySelectorAll('.intent-chip')];
const CHAT_LOG_MAX = 24;
const CHAT_PERSISTED = new Set(['user', 'assistant', 'sources']);
const ASSISTANT_WIDTH_MIN = 320;
const ASSISTANT_WIDTH_MAX = 1200;
const ASSISTANT_DOC_MIN = 360;
const ASSISTANT_OFFLINE = 'The Spec Assistant is not reachable. Run `specky serve` in this repo, then try again.';

function chatNewSessionId() {
  // crypto.randomUUID() needs a secure context, which http:// on a real hostname isn't.
  return crypto?.randomUUID ? crypto.randomUUID()
    : `s-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

let chatSession = tabStore?.getItem('specky-chat-session') || chatNewSessionId();
tabStore?.setItem('specky-chat-session', chatSession);

function readChatLog() {
  try {
    return JSON.parse(tabStore?.getItem('specky-chat-log') || '[]');
  } catch (err) {
    return [];
  }
}

function setAssistantOpen(open) {
  document.body.classList.toggle('assistant-open', open);
  tabStore?.setItem('specky-assistant-open', open ? '1' : '0');
  if (open) chatInput?.focus();
}

// Opening the panel hands it the nav rail's width; closing gives back whatever the rail was before.
// Only the click does this — reopening on the next page leaves the rail as the reader last set it,
// so one who brought the nav back mid-conversation keeps it.
chatToggle?.addEventListener('click', () => {
  tabStore?.setItem('specky-nav-before-assistant', isNavCollapsed() ? '1' : '0');
  setNavCollapsed(true);
  setAssistantOpen(true);
});
assistantClose?.addEventListener('click', () => {
  setAssistantOpen(false);
  setNavCollapsed(tabStore?.getItem('specky-nav-before-assistant') === '1');
});
if (tabStore?.getItem('specky-assistant-open') === '1') setAssistantOpen(true);

// --- panel width: one custom property on <html>, dragged and remembered ------------------
// A drag stops where the doc column would drop below ASSISTANT_DOC_MIN beside the rail, when it's
// showing. Overlaid on a narrow window (the 1100px media query) the panel covers the page rather
// than sharing the row, so only the window bounds it.
function assistantWidthMax() {
  const overlaid = getComputedStyle(assistantPanel).position === 'fixed';
  const reserved = overlaid ? 0 : (navRail?.getBoundingClientRect().width || 0) + ASSISTANT_DOC_MIN;
  return Math.max(ASSISTANT_WIDTH_MIN, Math.min(ASSISTANT_WIDTH_MAX, window.innerWidth - reserved));
}

function applyAssistantWidth(px, max) {
  const width = Math.min(Math.max(Math.round(px), ASSISTANT_WIDTH_MIN), max);
  document.documentElement.style.setProperty('--assistant-width', `${width}px`);
  return width;
}

function setAssistantWidth(px) {
  tabStore?.setItem('specky-assistant-width', String(applyAssistantWidth(px, assistantWidthMax())));
}

// Restored as saved, not re-capped: this window's cap depends on its size and on the rail, and
// saving that back would shrink the width for every page after this one. CSS's max-width keeps a
// wide saved width off the doc column in the meantime.
const storedAssistantWidth = Number(tabStore?.getItem('specky-assistant-width'));
if (storedAssistantWidth) applyAssistantWidth(storedAssistantWidth, ASSISTANT_WIDTH_MAX);

assistantResize?.addEventListener('pointerdown', (event) => {
  event.preventDefault();
  // Capture keeps a fast drag that outruns the 7px handle on target; window listeners are what
  // actually move the panel, so a browser that refuses the capture still resizes.
  try { assistantResize.setPointerCapture(event.pointerId); } catch (err) { /* not capturable */ }
  const onMove = (move) => setAssistantWidth(window.innerWidth - move.clientX);
  const stop = () => {
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', stop);
    window.removeEventListener('pointercancel', stop);
  };
  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', stop);
  window.addEventListener('pointercancel', stop);
});

assistantResize?.addEventListener('keydown', (event) => {
  const step = event.key === 'ArrowLeft' ? 24 : event.key === 'ArrowRight' ? -24 : 0;
  if (!step) return;
  event.preventDefault();
  setAssistantWidth(assistantPanel.getBoundingClientRect().width + step);
});

// --- intent: Auto lets the server classify the question; the other two pin it -------------
let assistantIntent = tabStore?.getItem('specky-assistant-intent') || 'auto';

function setAssistantIntent(value) {
  assistantIntent = value;
  tabStore?.setItem('specky-assistant-intent', value);
  for (const chip of intentChips) {
    chip.dataset.active = chip.dataset.intent === value ? 'true' : 'false';
  }
}
setAssistantIntent(assistantIntent);
for (const chip of intentChips) {
  chip.addEventListener('click', () => setAssistantIntent(chip.dataset.intent));
}

// The thinking line shows only while there's a status to show; an empty one hides it.
function setChatStatus(text) {
  if (chatStatus) chatStatus.textContent = text;
  if (chatThinking) chatThinking.hidden = !text;
}

function persistChatEntry(role, text) {
  if (!tabStore || !CHAT_PERSISTED.has(role)) return;
  const log = readChatLog();
  log.push({ role, text });
  tabStore.setItem('specky-chat-log', JSON.stringify(log.slice(-CHAT_LOG_MAX)));
}

function addChatMessage(role, text, persist = true) {
  const div = document.createElement('div');
  div.className = `chat-msg chat-${role}`;
  div.textContent = text;
  chatLog.appendChild(div);
  chatLog.scrollTop = chatLog.scrollHeight;
  if (persist) persistChatEntry(role, text);
  return div;
}

// A cited doc path is a page this site already rendered, so it should be one click away rather
// than a string the reader has to find in the nav. A commit sha (or a path from an index this
// page predates) has no entry here and stays plain text.
function sourceLink(source) {
  const hit = SPECKY_INDEX.find((d) => d.path === source);
  if (!hit) {
    const span = document.createElement('span');
    span.textContent = source;
    return span;
  }
  const link = document.createElement('a');
  link.className = 'chat-source-link';
  link.href = hit.html_path;
  link.textContent = hit.title;
  link.title = source;
  return link;
}

function copyMarkdownButton(markdown) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'chat-copy';
  button.textContent = 'Copy markdown';
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(markdown);
    } catch (err) {
      // clipboard.writeText needs a secure context, which a file:// page isn't.
      const area = document.createElement('textarea');
      area.value = markdown;
      document.body.appendChild(area);
      area.select();
      document.execCommand('copy');
      area.remove();
    }
    button.textContent = 'Copied';
    setTimeout(() => { button.textContent = 'Copy markdown'; }, 1500);
  });
  return button;
}

function appendSources(actions, sources) {
  if (!sources.length) return;
  const label = document.createElement('span');
  label.textContent = 'Sources:';
  actions.appendChild(label);
  sources.forEach((source, i) => {
    actions.appendChild(sourceLink(source));
    if (i < sources.length - 1) {
      const comma = document.createElement('span');
      comma.textContent = ',';
      actions.appendChild(comma);
    }
  });
}

// The answer HTML was rendered and sanitized by the server (see answer_render.py), which is the
// only reason this assigns innerHTML at all — the markdown behind it is the model's, and nothing
// on this side of the wire is in a position to vet markup.
function addChatAnswer(data) {
  const div = document.createElement('div');
  div.className = 'chat-msg chat-assistant chat-rich';
  div.innerHTML = data.answer_html || '';
  addDiagramButtons(div);
  chatLog.appendChild(div);

  // The short answer stands on its own (chat_server.split_answer); the details wait behind a toggle,
  // so a reader who already has what they came for isn't made to scroll past the rest of it.
  if (data.details_html) {
    const details = document.createElement('div');
    details.className = 'chat-details';
    details.hidden = true;
    details.innerHTML = data.details_html;
    addDiagramButtons(details);
    div.appendChild(details);
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'chat-more';
    more.textContent = 'Read more';
    more.setAttribute('aria-expanded', 'false');
    more.addEventListener('click', () => {
      details.hidden = !details.hidden;
      more.textContent = details.hidden ? 'Read more' : 'Show less';
      more.setAttribute('aria-expanded', details.hidden ? 'false' : 'true');
    });
    chatLog.appendChild(more);
  }

  const actions = document.createElement('div');
  actions.className = 'chat-actions';
  const badge = document.createElement('span');
  badge.className = 'intent-badge';
  badge.dataset.intent = data.intent || 'explore';
  badge.textContent = data.intent === 'spec' ? 'Draft spec' : 'Explore';
  actions.appendChild(badge);
  actions.appendChild(copyMarkdownButton(data.answer || ''));
  const sources = data.sources || [];
  appendSources(actions, sources);
  chatLog.appendChild(actions);
  chatLog.scrollTop = chatLog.scrollHeight;

  persistChatEntry('assistant', data.answer || '');
  if (sources.length) persistChatEntry('sources', `Sources: ${sources.join(', ')}`);
}

chatForm?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const question = chatInput.value.trim();
  if (!question) return;
  mentionDropdown.innerHTML = '';
  addChatMessage('user', question);
  chatInput.value = '';
  // A draft waiting on the reader takes what they type as its answer or correction — unless they
  // pinned Explore, which is how to ask the docs something mid-draft without derailing it.
  if (draftWaiting()) {
    await sendDraft('reply', { reply: question });
    return;
  }
  setChatStatus(assistantIntent === 'spec' ? DRAFT_STATUS.scope : 'Thinking…');
  const payload = { question, session: chatSession };
  if (assistantIntent !== 'auto') payload.intent = assistantIntent;
  try {
    const res = await speckyFetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    setChatStatus('');
    if (!res.ok) {
      addChatMessage('error', data.error || 'Something went wrong.');
      return;
    }
    if (data.draft) {
      showDraft(data);
      return;
    }
    addChatAnswer(data);
  } catch (err) {
    setChatStatus('');
    addChatMessage('error', ASSISTANT_OFFLINE);
  }
});

chatReset?.addEventListener('click', async () => {
  // Clear the panel and the id first: whether the server hears about it or not, the reader asked
  // for a blank slate, and a new id means the old transcript can't be reached again anyway.
  const previous = chatSession;
  chatLog.innerHTML = '';
  tabStore?.removeItem('specky-chat-log');
  clearDraft();
  chatSession = chatNewSessionId();
  tabStore?.setItem('specky-chat-session', chatSession);
  chatInput.focus();
  try {
    await speckyFetch('/chat/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session: previous }),
    });
  } catch (err) { /* offline, or no server: the abandoned transcript expires on its own */ }
});

// Replay what this conversation said on the pages before this one — as text, never as markup.
// What's stored is the model's markdown, and sessionStorage is editable by anything running on this
// origin, so replaying it through innerHTML would hand that anything a way into the page. The
// transcript is worth keeping; a second page's worth of rendered tables is not worth that.
if (chatLog) {
  let replayed = 0;
  for (const msg of readChatLog()) {
    // Storage is editable, so trust the shape as far as it checks out and drop the rest rather
    // than rendering a `chat-undefined` bubble.
    if (CHAT_PERSISTED.has(msg?.role) && typeof msg.text === 'string') {
      addChatMessage(msg.role, msg.text, false);
      replayed += 1;
    }
  }
  if (replayed) {
    const note = document.createElement('div');
    note.className = 'chat-note';
    note.textContent = 'Earlier answers are replayed as plain text. The next one renders in full.';
    chatLog.appendChild(note);
    chatLog.scrollTop = chatLog.scrollHeight;
  }
}
"""

# The draft-spec workflow's half of the panel (chat_server's `/draft`, spec_draft.py on the server).
# A draft's state comes back with every response and lives in sessionStorage as `specky-draft`, so it
# survives a page navigation and a `specky serve` restart alike, and is sent back with the next step.
#
# Every step but the last is structured data — a question and its options, the impact's two tables,
# the acceptance rows — and is built here with DOM calls and `textContent`, never `innerHTML`. That
# is what lets the current step be rebuilt from storage on the next page (storage is editable by
# anything on this origin; see the replay note in CHAT_JS). Only the final draft is markup, rendered
# and sanitized by the server, and it is never replayed from storage.
DRAFT_JS = """
const draftReply = document.getElementById('draft-reply');
const draftCancel = document.getElementById('draft-cancel');
const DRAFT_STEPS = [['scope', 'Scope'], ['impact', 'Impact'], ['acceptance', 'Tests'], ['final', 'Draft']];
const DRAFT_STATUS = {
  scope: 'Finding where this belongs…',
  impact: 'Working out what changes…',
  acceptance: 'Writing acceptance tests…',
  final: 'Writing the draft…',
};
const CHAT_PLACEHOLDER = chatInput?.placeholder || '';

function readDraft() {
  try {
    const state = JSON.parse(tabStore?.getItem('specky-draft') || 'null');
    return state && typeof state === 'object' && typeof state.request === 'string' ? state : null;
  } catch (err) {
    return null;
  }
}

let draftState = readDraft();
let draftBusy = false;

function draftWaiting() {
  return Boolean(draftState) && assistantIntent !== 'explore';
}

function syncDraftReply() {
  const waiting = draftWaiting();
  if (draftReply) draftReply.hidden = !waiting;
  if (!chatInput) return;
  if (!waiting) chatInput.placeholder = CHAT_PLACEHOLDER;
  else if (draftState.stage === 'question') chatInput.placeholder = 'Type your answer, or pick an option above…';
  else chatInput.placeholder = 'Type a correction and this step runs again…';
}

function saveDraft(state) {
  draftState = state;
  if (state) tabStore?.setItem('specky-draft', JSON.stringify(state));
  else tabStore?.removeItem('specky-draft');
  syncDraftReply();
}

function clearDraft() {
  saveDraft(null);
  retireDraftCards();
}

// Only the newest card's buttons act: an older card's "Approve" would send a state the draft has
// already moved past.
function retireDraftCards() {
  for (const button of chatLog?.querySelectorAll('.draft-card .draft-action') || []) {
    button.disabled = true;
  }
}

function reviveLatestDraftCard() {
  const cards = chatLog?.querySelectorAll('.draft-card') || [];
  const latest = cards[cards.length - 1];
  for (const button of latest?.querySelectorAll('.draft-action') || []) button.disabled = false;
}

// A question belongs to the step that asked it: scope's "where does this go", or impact's "which
// reading of this behaviour did you mean".
function draftActiveStage(state) {
  if (state?.stage !== 'question') return state?.stage;
  return state.question?.resume === 'impact' ? 'impact' : 'scope';
}

function draftStatusFor(action) {
  if (action === 'confirm') return DRAFT_STATUS.acceptance;
  if (action === 'approve') return DRAFT_STATUS.final;
  if (action === 'rescope') return DRAFT_STATUS.scope;
  if (action === 'choose') return DRAFT_STATUS.impact;
  return DRAFT_STATUS[draftActiveStage(draftState)] || 'Thinking…';
}

async function sendDraft(action, extra = {}) {
  if (!draftState || draftBusy) return;
  draftBusy = true;
  retireDraftCards();
  setChatStatus(draftStatusFor(action));
  try {
    const res = await speckyFetch('/draft', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, state: draftState, ...extra }),
    });
    const data = await res.json();
    setChatStatus('');
    if (!res.ok) {
      addChatMessage('error', data.error || 'Something went wrong.');
      reviveLatestDraftCard();
      return;
    }
    showDraft(data);
  } catch (err) {
    setChatStatus('');
    addChatMessage('error', ASSISTANT_OFFLINE);
    reviveLatestDraftCard();
  } finally {
    draftBusy = false;
  }
}

function showDraft(data) {
  const state = data.draft;
  saveDraft(state);
  if (state.stage === 'final') addDraftFinal(data);
  else renderDraftCard(state);
  persistChatEntry('assistant', draftSummary(state, data));
}

// What the transcript keeps of a step: one line of text. The step itself is rebuilt from state.
function draftSummary(state, data) {
  const where = state.scope?.path || 'a new doc';
  if (state.stage === 'question') return `Draft · ${state.question?.text || 'a question'}`;
  if (state.stage === 'impact') return `Draft · what changes in ${where}: ${state.impact?.summary || ''}`;
  if (state.stage === 'acceptance') return `Draft · ${state.tests.length} acceptance tests for ${where}`;
  return data.answer || `Draft · ${where}`;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function draftButton(label, onClick, primary = false) {
  const button = el('button', primary ? 'draft-action primary' : 'draft-action', label);
  button.type = 'button';
  button.addEventListener('click', onClick);
  return button;
}

function draftSteps(state) {
  const current = draftActiveStage(state);
  const at = DRAFT_STEPS.findIndex(([key]) => key === current);
  const row = el('div', 'draft-steps');
  DRAFT_STEPS.forEach(([_key, label], i) => {
    const step = el('span', 'draft-step', `${i + 1} ${label}`);
    step.dataset.state = i < at ? 'done' : i === at ? 'active' : 'todo';
    row.appendChild(step);
  });
  return row;
}

function draftTarget(scope) {
  const line = el('p', 'draft-target', 'Target: ');
  line.appendChild(el('code', '', scope.path || ''));
  line.appendChild(el('span', 'draft-kind', `${scope.existing ? 'update' : 'new'} ${scope.type || 'feature'}`));
  return line;
}

function draftTable(headers, rows) {
  const figure = el('figure', 'tw');
  const table = document.createElement('table');
  const head = table.createTHead().insertRow();
  for (const header of headers) head.appendChild(el('th', '', header));
  const body = table.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    for (const cell of row) {
      const td = tr.insertCell();
      if (cell instanceof Node) td.appendChild(cell);
      else td.textContent = cell ?? '';
    }
  }
  figure.appendChild(table);
  return figure;
}

function kindLabel(text, kind) {
  const cell = el('span', '', text || '');
  if (kind) cell.appendChild(el('span', `draft-kind-${kind}`, ` (${kind})`));
  return cell;
}

// A new step's card: the older cards' buttons retired, then the progress row and the target.
function newDraftCard(state) {
  retireDraftCards();
  const card = el('div', 'chat-msg chat-assistant chat-rich draft-card');
  card.appendChild(draftSteps(state));
  if (state.scope) card.appendChild(draftTarget(state.scope));
  return card;
}

function renderDraftCard(state) {
  const card = newDraftCard(state);
  if (state.stage === 'question') {
    card.appendChild(el('p', '', state.question?.text || ''));
    const options = el('div', 'draft-options');
    (state.question?.options || []).forEach((option, i) => {
      options.appendChild(draftButton(option.label, () => sendDraft('choose', { option: i })));
    });
    card.appendChild(options);
    card.appendChild(el('p', 'draft-hint', 'Or type your own answer below.'));
  } else if (state.stage === 'impact') {
    const impact = state.impact || {};
    if (impact.summary) card.appendChild(el('p', '', impact.summary));
    if ((impact.changes || []).length) {
      card.appendChild(el('h4', 'draft-card-title', 'Changes'));
      card.appendChild(draftTable(
        ['Section', 'Change', 'What'],
        impact.changes.map((c) => [c.section, el('span', `draft-kind-${c.kind}`, c.kind), c.summary]),
      ));
    }
    if ((impact.behaviours || []).length) {
      card.appendChild(el('h4', 'draft-card-title', 'Behaviours that change'));
      card.appendChild(draftTable(
        ['Behaviour', 'Today', 'After', 'Ref'],
        impact.behaviours.map((b) => [kindLabel(b.behaviour, b.kind), b.today, b.after, b.ref || '—']),
      ));
    }
    const actions = el('div', 'draft-actions');
    actions.appendChild(draftButton('Write acceptance tests', () => sendDraft('confirm'), true));
    actions.appendChild(draftButton('Change module', () => sendDraft('rescope')));
    card.appendChild(actions);
    card.appendChild(el('p', 'draft-hint', 'Something off? Type a correction below and this step runs again.'));
  } else if (state.stage === 'acceptance') {
    card.appendChild(el('h4', 'draft-card-title', 'Acceptance tests'));
    card.appendChild(draftTable(
      ['Scenario', 'Given', 'When', 'Then', 'Covers'],
      (state.tests || []).map((t) => [t.scenario, t.given, t.when, t.then, t.covers]),
    ));
    const actions = el('div', 'draft-actions');
    actions.appendChild(draftButton('Approve tests', () => sendDraft('approve'), true));
    card.appendChild(actions);
    card.appendChild(el('p', 'draft-hint', 'Type a correction below to revise them before approving.'));
  }
  chatLog.appendChild(card);
  chatLog.scrollTop = chatLog.scrollHeight;
}

// The finished draft — the one step whose body is markup, rendered and sanitized by the server
// (spec_draft._run_final → answer_render). Nothing is written to the docs tree: Copy is the output.
function addDraftFinal(data) {
  const state = data.draft;
  const card = newDraftCard(state);
  for (const warning of data.warnings || []) card.appendChild(el('div', 'draft-warning', warning));
  const body = el('div');
  body.innerHTML = data.answer_html || '';
  addDiagramButtons(body);
  card.appendChild(body);

  const actions = el('div', 'chat-actions');
  actions.appendChild(copyMarkdownButton(data.answer || ''));
  let diff = null;
  if (data.diff) {
    diff = el('pre', 'draft-diff', data.diff);
    diff.hidden = true;
    const toggle = el('button', 'chat-copy', `Show what changes in ${state.scope?.path || 'the doc'}`);
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', 'false');
    toggle.addEventListener('click', () => {
      diff.hidden = !diff.hidden;
      toggle.setAttribute('aria-expanded', diff.hidden ? 'false' : 'true');
    });
    actions.appendChild(toggle);
  }
  appendSources(actions, data.sources || []);
  card.appendChild(actions);
  if (diff) card.appendChild(diff);
  card.appendChild(el('p', 'draft-hint', 'Type a correction below to revise the draft, or cancel the draft when you are done.'));
  chatLog.appendChild(card);
  chatLog.scrollTop = chatLog.scrollHeight;
}

draftCancel?.addEventListener('click', () => {
  clearDraft();
  addChatMessage('note', 'Draft cancelled.', false);
  chatInput?.focus();
});
// CHAT_JS's own listener sets the intent first; this one runs after it and re-reads it.
for (const chip of intentChips) chip.addEventListener('click', syncDraftReply);

// A draft in progress on the page before this one: rebuild its current step from state, so its
// buttons still work. A finished draft isn't rebuilt — its markup is never replayed from storage —
// but it stays open for corrections.
if (draftState && chatLog && draftState.stage !== 'final') {
  try {
    renderDraftCard(draftState);
  } catch (err) {
    saveDraft(null);
  }
}
syncDraftReply();
"""

# '#' mention autocomplete for the chat input: candidates come straight from SPECKY_INDEX
# (already embedded for the sidebar search box), so this needs no server round-trip and no
# new data island — one entry per domain ("module") plus one per feature/workflow doc.
MENTION_JS = """
const mentionDropdown = document.getElementById('mention-dropdown');

const MENTION_CANDIDATES = (() => {
  const seenModules = new Set();
  const out = [];
  for (const d of SPECKY_INDEX) {
    if (!seenModules.has(d.domain)) {
      seenModules.add(d.domain);
      out.push({ kind: 'module', value: d.domain, label: d.domain });
    }
  }
  for (const d of SPECKY_INDEX) {
    if (d.doc_type === 'feature' || d.doc_type === 'workflow') {
      out.push({ kind: 'feature', value: d.slug, label: d.title });
    }
  }
  return out;
})();

let mentionMatchStart = -1;
let mentionMatchEnd = -1;

function currentMentionQuery() {
  const caret = chatInput.selectionStart ?? chatInput.value.length;
  const upToCaret = chatInput.value.slice(0, caret);
  const match = /#([\\w-]*)$/.exec(upToCaret);
  if (!match) return null;
  return { query: match[1].toLowerCase(), start: match.index, end: caret };
}

function renderMentionDropdown(candidates) {
  mentionDropdown.innerHTML = '';
  for (const c of candidates.slice(0, 8)) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'mention-item';
    btn.innerHTML = `<span>${c.label}</span><span class="kind">${c.kind}</span>`;
    btn.addEventListener('click', () => selectMention(c));
    mentionDropdown.appendChild(btn);
  }
}

function selectMention(candidate) {
  const token = `#${candidate.kind}:${candidate.value} `;
  const value = chatInput.value;
  chatInput.value = value.slice(0, mentionMatchStart) + token + value.slice(mentionMatchEnd);
  mentionDropdown.innerHTML = '';
  chatInput.focus();
  const caret = mentionMatchStart + token.length;
  chatInput.setSelectionRange(caret, caret);
}

chatInput?.addEventListener('input', () => {
  const state = currentMentionQuery();
  if (!state) {
    mentionDropdown.innerHTML = '';
    mentionMatchStart = -1;
    mentionMatchEnd = -1;
    return;
  }
  mentionMatchStart = state.start;
  mentionMatchEnd = state.end;
  const hits = MENTION_CANDIDATES.filter((c) => c.label.toLowerCase().includes(state.query) || c.value.toLowerCase().includes(state.query));
  renderMentionDropdown(hits);
});

chatInput?.addEventListener('keydown', (event) => {
  if (mentionDropdown.children.length === 0) return;
  if (event.key === 'Escape') {
    mentionDropdown.innerHTML = '';
  } else if (event.key === 'Enter') {
    event.preventDefault();
    mentionDropdown.firstElementChild?.click();
  }
});
"""

# Ported from glia's `_TOOLTIP_SCRIPT` (enrichment_render.py) — same hover, minus the
# host-messaging half glia needs for its sandboxed-iframe pages, which a plain page here
# doesn't. One tooltip element reused for every hover, not one per term.
#
# Listeners are delegated from `document` rather than attached per term at load: a chat answer is
# glossary-linked too (see answer_render.render_answer) and arrives long after this script ran, so
# per-element binding would give the page's own terms a tooltip and an answer's terms none.
GLOSSARY_JS = """
const tip = document.createElement('div');
tip.className = 'tip';
tip.setAttribute('role', 'tooltip');
tip.hidden = true;
document.body.appendChild(tip);

function showGlossaryTip(target) {
  const key = (target.dataset.term || '').toLowerCase();
  const entry = SPECKY_GLOSSARY[key];
  if (!entry) return;
  tip.textContent = entry;
  tip.hidden = false;
  const box = target.getBoundingClientRect();
  tip.style.top = (box.bottom + window.scrollY + 6) + 'px';
  tip.style.left = Math.max(8, box.left + window.scrollX) + 'px';
}
function hideGlossaryTip() { tip.hidden = true; }

for (const type of ['mouseover', 'focusin']) {
  document.addEventListener(type, (event) => {
    const target = event.target.closest?.('[data-term]');
    if (target) showGlossaryTip(target);
  });
}
for (const type of ['mouseout', 'focusout']) {
  document.addEventListener(type, (event) => {
    if (event.target.closest?.('[data-term]')) hideGlossaryTip();
  });
}
"""

# One file, in this order, deliberately: SEARCH_JS reads `activeTags`/`activeType` (declared by
# FILTER_JS) and calls `speckyFetch` (API_JS), which CHAT_JS also calls; CHAT_JS calls DIAGRAM_JS's
# `addDiagramButtons`, which uses SEARCH_JS's `escapeHtml` — same script scope, so those top-level
# declarations resolve by the time an event handler runs. CHAT_JS and DRAFT_JS call into each other
# (`draftWaiting`/`showDraft` one way, `appendSources`/`copyMarkdownButton` the other) from event
# handlers only; DRAFT_JS's one load-time step, rebuilding a draft in progress, runs after CHAT_JS
# has declared everything it touches. Splitting these into separate <script> tags would break that.
APP_JS_BLOCKS = (
    API_JS, SEARCH_JS, FILTER_JS, NAV_JS, CHAT_JS, DRAFT_JS, MENTION_JS, GLOSSARY_JS,
    diagram_render.DIAGRAM_JS,
)

_DOMAIN_ORDER_FIRST = "root"
_DOMAIN_ORDER_LAST = "history"

# Curated icon per known domain (directory under specs/); any domain not listed here
# (domains aren't a closed set — they're just directory names) falls back to a generic
# folder icon rather than guessing.
_DOMAIN_ICONS = {
    "root": "home",
    "chat": "chat",
    "cli": "terminal",
    "docs": "book",
    "documentation": "file-text",
    "history": "clock",
    "rendering": "image",
    "search": "search",
}
_DOMAIN_ICON_FALLBACK = "folder"

# A generated doc is titled `# <Domain> — <Topic>` (and a root doc `# <repo> — <Topic>`, see
# generator.py), which reads right on its own page but repeats the group header in the sidebar.
# Spaces are required around the dash so a hyphenated word is never split.
_TITLE_PREFIX = re.compile(r"^(?P<prefix>.+?)\s+[—–-]\s+(?P<rest>\S.*)$")

# Fixed categorical palette for tags (see --tag-N/-N-bg in CSS). A tag's color is a stable
# hash of its name, not configuration — the actual tag vocabulary is small and open-ended
# (specs/documentation/feature-classification-and-tags.md), so there's nothing to configure.
_TAG_COLOR_COUNT = 8


def _icon_for_domain(domain: str) -> str:
    return _DOMAIN_ICONS.get(domain, _DOMAIN_ICON_FALLBACK)


def _icon_for_type(doc_type: str) -> str:
    return _TYPE_ICONS.get(doc_type, _TYPE_ICON_FALLBACK)


def _title_key(text: str) -> str:
    """Case and punctuation folded away, so `feature-flags` and "Feature Flags" compare equal."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _nav_title(title: str, domain: str, project: str) -> str:
    """`title` without a leading `<module> — `, for the sidebar where the module is the group.

    Stripped only when the prefix *is* the module (or, for a root doc, the repo name) — a title
    that happens to contain a dash for some other reason is left exactly as written.
    """
    match = _TITLE_PREFIX.match(title)
    owner = project if domain == "root" else domain
    if match and _title_key(match.group("prefix")) == _title_key(owner):
        return match.group("rest")
    return title


def _tag_class(tag: str) -> str:
    return f"tag-{int(hashlib.sha1(tag.encode()).hexdigest(), 16) % _TAG_COLOR_COUNT}"


def domain_sort_key(domain: str) -> tuple[int, str]:
    """Root docs first, per-commit history last, everything else alphabetical between them.

    Shared with `specky export` so a single-page export reads in the same order as the viewer's
    sidebar — two orders for the same docs is the kind of difference a reader notices and can't
    explain.
    """
    if domain == _DOMAIN_ORDER_FIRST:
        return (0, domain)
    if domain == _DOMAIN_ORDER_LAST:
        return (2, domain)
    return (1, domain)


def slug(doc_path: str) -> str:
    """A doc's repo-relative path flattened into one page name.

    Uses the *whole* path under the docs root, not just `<domain>-<stem>`: a domain is only the
    first path component (see `indexer._domain_for`), so `specs/a/b/x.md` and `specs/a/c/x.md`
    share a domain and a stem, and keying on those two alone made the second page overwrite the
    first and silently vanish from the site. Root-level docs keep the `root-` prefix their domain
    already gives them, which also keeps `specs/index.md` from colliding with the home page.
    """
    parts = Path(doc_path).with_suffix("").parts[1:]  # [1:] drops the docs root's own name
    if len(parts) == 1:
        parts = ("root",) + parts
    return "-".join(re.sub(r"[^A-Za-z0-9]+", "-", p).strip("-") for p in parts)


def _excerpt(content: str, length: int = 160) -> str:
    text = _plain_text(content, drop_first_heading=True)
    return f"{text[:length]}…" if len(text) > length else text


def _plain_text(content: str, drop_first_heading: bool = False) -> str:
    """Markdown reduced to the words a reader would search for: no syntax, no runs of space."""
    text = (
        re.sub(r"^#.*$", "", content, count=1, flags=re.MULTILINE)
        if drop_first_heading
        else content
    )
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # `_` survives, unlike the other markdown punctuation: it's emphasis syntax perhaps once per
    # doc, and part of an identifier constantly (`busy_timeout`, `run_index`) — and an identifier
    # is what a reader of these docs actually types into the search box.
    text = re.sub(r"[#*`>|]", "", text)
    return " ".join(text.split())


# --- the in-page search payload ---------------------------------------------------------
# `assets/site-data.js` is a plain <script src> that *every* page loads eagerly, so its size is
# paid on each navigation, and on file:// there is no gzip and no lazy loading to hide it. What
# blows this up is doc *count*, not doc size: a repo with 3,000 commits has ~3,000 specs/history/
# docs before it has a single feature doc. So the payload gets a total budget and the per-doc cap
# adapts to it, instead of a fixed per-doc cap that bounds nothing.
SEARCH_BODY_TOTAL_CHARS = 1_000_000
SEARCH_BODY_MAX_CHARS = 8_000
SEARCH_BODY_MIN_CHARS = 400


def search_body_cap(doc_count: int) -> int | None:
    """Chars of body text to ship per doc, or None to ship none at all.

    Small repo → whole docs. ~500 docs → ~2 KB each. Past the point where even the floor
    wouldn't fit the budget (~2,500 docs), None: the viewer falls back to excerpt-only matching,
    which is what it did before bodies existed. A 4 MB blocking script on every page navigation
    would be a worse answer than a weaker offline search, and a repo that big is exactly the one
    that should be searching through `specky serve` against the FTS5 index anyway.
    """
    if doc_count <= 0 or doc_count * SEARCH_BODY_MIN_CHARS > SEARCH_BODY_TOTAL_CHARS:
        return None
    return min(SEARCH_BODY_MAX_CHARS, max(SEARCH_BODY_MIN_CHARS, SEARCH_BODY_TOTAL_CHARS // doc_count))


def _search_body(content: str, cap: int) -> str:
    """Match-only text: lowercased at build time so the client isn't lowercasing every doc on
    every keystroke, and truncated to `cap` — the tail of a long doc is the part that stops being
    findable offline, which is the trade the budget above buys."""
    return _plain_text(content)[:cap].lower()


# --- tables ---------------------------------------------------------------------------

_TABLE = re.compile(r"<table>(.*?)</table>", re.DOTALL)


def _wrap_tables(body_html: str) -> str:
    """Wrap `tables` extension output in a scrollable figure — see `figure.tw` in CSS."""
    return _TABLE.sub(r'<figure class="tw"><table>\1</table></figure>', body_html)


# --- workflow steps ---------------------------------------------------------------------
# A workflow doc's `## How It Works` is its happy path in order, and the order is the whole reason
# the doc exists — so it gets a stepper rather than the same `<ol>` every other list on the page
# gets. Only for `doc_type == "workflow"`: on a feature doc the numbered list is one detail among
# several and promoting it would just be louder, not clearer.

# `head` is tempered against `h2` rather than a plain `.*?`: left greedy-free but unbounded, the
# first `<h2>` on the page swallows everything up to the *steps*' closing tag, the heading check
# then fails on the concatenation, and the real heading never gets its own attempt.
_STEP_SECTION = re.compile(
    r"(<h2>(?P<head>(?:(?!</?h2>).)*?)</h2>\s*)<ol>(?P<items>.*?)</ol>", re.DOTALL
)
_STEP_ITEM = re.compile(r"<li>\s*(?P<body>.*?)\s*</li>", re.DOTALL)
# The bold label the template asks for, and whatever separator the writer put after it. The label
# may itself contain markup — `link_glossary` has already run, so a glossary term inside it is a
# `<span class="gl">` by now.
_STEP_LABEL = re.compile(r"^<strong>(?P<label>.*?)</strong>\s*[-—–:]?\s*(?P<rest>.*)$", re.DOTALL)
# A list whose items are separated by blank lines is a *loose* list, and markdown wraps each item's
# text in its own `<p>`. That is how most of these docs are written, so matching only the tight form
# would leave the stepper off more docs than it reached.
_LONE_PARAGRAPH = re.compile(r"^<p>(?P<text>(?:(?!</?p>).)*)</p>$", re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")
# A step carrying a sub-list is left alone entirely. `_STEP_ITEM`'s lazy `.*?</li>` would otherwise
# stop at the *nested* `</li>`, closing the description span inside the sub-list and spilling the
# leftover `</ul></li>` into the page as stray tags.
_NESTED_LIST = re.compile(r"<[uo]l[ >]")
STEP_HEADING = "how it works"


def _step_list(body_html: str) -> str:
    """Turn a workflow's happy-path `<ol>` into `<ol class="steps">` — see `ol.steps` in CSS.

    Left exactly as it was unless the list really is the shape the template asks for: under the
    `## How It Works` heading, and with bold labels to split on. A doc that writes its steps some
    other way gets a plain list rather than a stepper full of empty titles, which is the right
    trade — nothing here is load-bearing, and a hand-written doc predating the template is the
    common case.
    """

    def section(match: re.Match[str]) -> str:
        if _TAGS.sub("", match.group("head")).strip().lower() != STEP_HEADING:
            return match.group(0)
        if _NESTED_LIST.search(match.group("items")):
            return match.group(0)
        labelled = False

        def item(li: re.Match[str]) -> str:
            nonlocal labelled
            body = li.group("body")
            lone = _LONE_PARAGRAPH.match(body)
            # Unwrapped only when the item *is* one paragraph. An item carrying a second paragraph
            # keeps its markup, because splitting a label off the first one would leave the rest
            # orphaned outside the step it belongs to.
            parts = _STEP_LABEL.match(lone.group("text") if lone else body)
            if parts is None:
                return li.group(0)
            labelled = True
            rest = parts.group("rest").strip()
            return (
                f'<li><span class="st">{parts.group("label")}</span>'
                + (f'<span class="sd">{rest}</span>' if rest else "")
                + "</li>"
            )

        items = _STEP_ITEM.sub(item, match.group("items"))
        if not labelled:
            return match.group(0)
        return f'{match.group(1)}<ol class="steps">{items}</ol>'

    return _STEP_SECTION.sub(section, body_html)


# --- glossary auto-linking -------------------------------------------------------------
# Ported from glia's `link_glossary` (enrichment_render.py) — same whole-word, longest-
# term-wins, first-occurrence-per-page algorithm, over plain markdown output rather than
# a model-sanitized fragment.

_GLOSSARY_ROW = re.compile(r"^\|\s*\*\*([^*|]+)\*\*\s*\|\s*(.+?)\s*\|\s*$")
_TAG_OR_TEXT = re.compile(r"(<[^>]+>)")
# A term is not wrapped inside these: one in a code sample or diagram source is a
# literal, and one already inside a link or a marked span has its own behaviour.
_NO_WRAP_INSIDE = {"code", "pre", "a", "span", "button"}


def _strip_markdown(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return re.sub(r"[*`_]", "", text).strip()


def load_glossary(repo_root: Path) -> dict[str, str]:
    """`specs/GLOSSARY.md`'s `| **Term** | Definition |` rows as `{term: plain text}`.

    Empty if the repo has no glossary yet — auto-linking degrades to a no-op, same as a
    doc with no glossary terms to find.
    """
    path = paths.glossary(repo_root)
    if not path.exists():
        return {}
    terms: dict[str, str] = {}
    for line in path.read_text().splitlines():
        match = _GLOSSARY_ROW.match(line)
        if not match:
            continue
        terms[match.group(1).strip()] = _strip_markdown(match.group(2))
    return terms


def _terms_pattern(terms: list[str]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(t) for t in terms)
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


def link_glossary(fragment: str, glossary: dict[str, str]) -> str:
    """Wrap the first occurrence of each glossary term in a hover span.

    Longest term first, so e.g. "feature/workflow doc" wins over a bare "feature" where
    both are defined. First occurrence only — marking every mention turns a paragraph into
    a field of underlines.
    """
    ordered = sorted({t.strip() for t in glossary if t.strip()}, key=len, reverse=True)
    if not ordered:
        return fragment
    pattern = _terms_pattern(ordered)
    seen: set[str] = set()
    depth = 0
    out: list[str] = []

    def wrap(match: re.Match[str]) -> str:
        word = match.group(0)
        key = word.lower()
        if key in seen:
            return word
        seen.add(key)
        # tabindex so the tooltip is reachable without a mouse. It's set here rather than by the
        # hover script, which delegates from `document` and so never touches the spans (see
        # GLOSSARY_JS).
        return (
            f'<span class="gl" data-term="{html.escape(word, quote=True)}" tabindex="0">'
            f"{word}</span>"
        )

    for token in _TAG_OR_TEXT.split(fragment):
        if token.startswith("<"):
            name = token.lstrip("</").split(" ")[0].rstrip(">/").lower()
            if name in _NO_WRAP_INSIDE:
                depth += -1 if token.startswith("</") else 1
                depth = max(depth, 0)
            out.append(token)
            continue
        if depth or not token:
            out.append(token)
            continue
        out.append(pattern.sub(wrap, token))
    return "".join(out)


# python-markdown's defaults leave a `|` table as a paragraph of pipes and a ``` fence as
# indented text, which are the two constructs every generated doc is made of.
MARKDOWN_EXTENSIONS = ["tables", "fenced_code"]


def markdown_html(content: str) -> str:
    return md.markdown(content, extensions=MARKDOWN_EXTENSIONS)


def render_doc_body(
    content: str, glossary: dict[str, str], doc_type: str = ""
) -> tuple[str, bool, bool]:
    """A doc's markdown as the HTML every specky renderer shows: glossary terms wrapped, tables in
    a scrollable figure, ```mermaid``` fences replaced by static SVG.

    Returns `(html, any mermaid source, any of it rendered)` — the two flags drive the one-time
    "run `specky setup-diagrams`" hint. `specky export` shares this so a stakeholder's single-page
    HTML or PDF is the same content as the viewer's, not a second renderer's guess at it.

    `doc_type` only ever adds the workflow stepper (`_step_list`). It defaults to "" so a caller
    with no classification to hand, and every doc that isn't a workflow, gets exactly the rendering
    it got before.
    """
    body_html = _wrap_tables(link_glossary(markdown_html(content), glossary))
    if doc_type == "workflow":
        body_html = _step_list(body_html)
    return diagram_render.render_mermaid_blocks(body_html)


# --- doc chrome: breadcrumb/tags header, and the "Related" cross-link section ---------


def _doc_header(doc: dict) -> str:
    icon = _icon_for_domain(doc["domain"])
    parts = [
        f'<div class="breadcrumb"><svg class="icon" aria-hidden="true">'
        f'<use href="#icon-{icon}"></use></svg>{html.escape(doc["domain"])}</div>'
    ]
    chips = []
    if doc["doc_type"]:
        type_icon = _icon_for_type(doc["doc_type"])
        label = "Feature" if doc["doc_type"] == "feature" else "Workflow"
        chips.append(
            f'<button class="chip type-{doc["doc_type"]}" type="button" data-facet="type" '
            f'data-type="{doc["doc_type"]}" data-active="false">'
            f'<svg class="icon" aria-hidden="true"><use href="#icon-{type_icon}"></use></svg>{label}</button>'
        )
    # First chip after the type badge, because "how far behind is this?" changes how the reader
    # should treat everything under it. Clicking it filters, like every other chip.
    if doc["stale_days"]:
        chips.append(
            '<button class="chip stale" type="button" data-facet="stale" data-active="false" '
            f'title="The code this doc covers changed {doc["stale_days"]} days after the doc did">'
            '<svg class="icon" aria-hidden="true"><use href="#icon-clock"></use></svg>'
            f'{doc["stale_days"]} days behind code</button>'
        )
    for tag in doc["tags"]:
        safe_tag = html.escape(tag, quote=True)
        chips.append(
            f'<button class="chip {_tag_class(tag)}" type="button" data-facet="tag" '
            f'data-tag="{safe_tag}" data-active="false">{html.escape(tag)}</button>'
        )
    if chips:
        parts.append(f'<div class="doc-tags">{"".join(chips)}</div>')
    # Plain text, not a mailto: or an @-link — `owner:` is free-form (a name, a team, a Slack
    # channel, a handle), and guessing which of those it is would produce broken links.
    if doc["owner"]:
        parts.append(
            '<div class="doc-owner"><svg class="icon" aria-hidden="true">'
            '<use href="#icon-person"></use></svg>Who to ask: '
            f"<strong>{html.escape(doc['owner'])}</strong></div>"
        )
    return "".join(parts)


def _related_section(doc: dict, path_lookup: dict[str, dict]) -> str:
    """`related:` slugs resolved to real links — dead data until now (see module docstring)."""
    items = []
    for slug in doc["related"]:
        target = path_lookup.get(f"{slug}.md")
        if not target:
            continue
        items.append(
            f'<li><a href="{target["html_name"]}">'
            f'<svg class="icon" aria-hidden="true"><use href="#icon-link"></use></svg>'
            f'{html.escape(target["title"])}</a></li>'
        )
    if not items:
        return ""
    return f'<div class="related"><h2>Related</h2><ul>{"".join(items)}</ul></div>'


def _home_body(
    doc_count: int, domain_count: int, feature_count: int, workflow_count: int, tag_chips: list[dict]
) -> str:
    stats = [
        ("docs", doc_count),
        ("domains", domain_count),
        ("features", feature_count),
        ("workflows", workflow_count),
    ]
    stat_html = "".join(
        f'<div class="stat"><span class="n">{n}</span><span class="label">{label}</span></div>'
        for label, n in stats
        if n
    )
    tag_cloud = ""
    if tag_chips:
        chip_html = "".join(
            f'<button class="chip {t["cls"]}" type="button" data-facet="tag" '
            f'data-tag="{html.escape(t["name"], quote=True)}" data-active="false">'
            f'{html.escape(t["name"])}</button>'
            for t in tag_chips
        )
        tag_cloud = f'<div class="tag-cloud"><h2>Browse by tag</h2><div class="chip-row">{chip_html}</div></div>'
    return (
        "<h1>specky docs</h1>"
        "<p>Auto-generated, browsable functional reference — no server required.</p>"
        f'<div class="stat-row">{stat_html}</div>'
        f"{tag_cloud}"
        '<p style="margin-top:24px" class="empty-state">Pick a doc from the left, or search above.</p>'
    )


def _render_rail(
    domains: dict[str, list[dict]],
    tag_chips: list[dict],
    has_features: bool,
    has_workflows: bool,
    has_stale: bool,
) -> str:
    """The sidebar, rendered once for the whole site — nothing in it varies per page (see
    NAV_JS for the active-link state, which does)."""
    ordered = [
        {
            "name": domain,
            "icon": _icon_for_domain(domain),
            "docs": sorted(domains[domain], key=lambda d: d["nav_title"]),
            # "history" entries are commit shas, not meaningful titles — collapsed by
            # default so they don't crowd out the rest of the nav. NAV_JS re-opens the
            # group when the page you're on is one of them.
            "open": domain != _DOMAIN_ORDER_LAST,
        }
        for domain in sorted(domains, key=domain_sort_key)
    ]
    return _RAIL_TEMPLATE.render(
        domains=ordered,
        all_tags=tag_chips,
        has_features=has_features,
        has_workflows=has_workflows,
        has_stale=has_stale,
    )


def _page(title: str, rail_html: str, body_html: str, doc_type: str = "") -> str:
    return _PAGE_TEMPLATE.render(title=title, rail=rail_html, body=body_html, doc_type=doc_type)


def _write_assets(site_dir: Path, search_entries: list[dict], hover: dict[str, str]) -> None:
    """The three files every page links to. Written once per render; see the module
    docstring for why they're separate files rather than inlined.

    Everything here is site-wide, so nothing in it needs the "</" escaping an inline
    <script> would: the JSON never lands inside HTML.
    """
    assets = site_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "site.css").write_text("\n".join((CSS, diagram_render.DIAGRAM_CSS)))
    (assets / "app.js").write_text("\n".join(APP_JS_BLOCKS))
    # ASCII-escaped on purpose: a <script src> carries no encoding of its own, so an em dash
    # in a doc title travels as \\uXXXX rather than relying on the page's charset reaching
    # the asset.
    (assets / "site-data.js").write_text(
        f"const SPECKY_INDEX = {json.dumps(search_entries)};\n"
        f"const SPECKY_GLOSSARY = {json.dumps(hover)};\n"
        f"const SPECKY_CHAT_PORT = {CHAT_PORT};\n"
    )


def render_site(repo_root: Path) -> Path:
    conn = connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT path, domain, title, content, doc_type, tags, related, owner, "
            "stale_since, last_code_change FROM documents ORDER BY domain, title"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        raise RuntimeError("no documents indexed yet — run `specky index` first")

    site_dir = repo_root / ".specky" / "site"
    if site_dir.exists():
        shutil.rmtree(site_dir)
    site_dir.mkdir(parents=True)

    glossary = load_glossary(repo_root)
    hover_map = {term.lower(): definition for term, definition in glossary.items()}

    body_cap = search_body_cap(len(rows))

    domains: dict[str, list[dict]] = {}
    search_entries = []
    docs = []
    path_lookup: dict[str, dict] = {}
    all_tags: set[str] = set()
    for row in rows:
        path, domain, title, content, doc_type, tags_raw, related_raw, owner = row[:8]
        stale_since, last_code = row[8:]
        html_name = f"{slug(path)}.html"
        tags = [t for t in tags_raw.split(",") if t]
        related = [r for r in related_raw.split(",") if r]
        all_tags.update(tags)
        # 0 for a doc staleness.py didn't flag, so every consumer can treat this as "days behind"
        # and stay uninterested in which of the two columns was empty.
        stale_days = days_behind(stale_since, last_code) if stale_since and last_code else 0
        doc = {
            "html_name": html_name,
            "domain": domain,
            "title": title,
            "content": content,
            "doc_type": doc_type,
            "tags": tags,
            "related": related,
            "owner": owner,
            "stale_days": stale_days,
        }
        domains.setdefault(domain, []).append(
            {
                "title": title,
                "nav_title": _nav_title(title, domain, repo_root.name),
                "html_name": html_name,
                "data_tags": ",".join(tags),
                "doc_type": doc_type,
                "type_icon": _icon_for_type(doc_type),
                "stale": "true" if stale_days else "false",
            }
        )
        docs.append(doc)
        # Keyed by `<domain>/<topic>.md`: that's how a `related:` entry names its target, whatever
        # the docs root happens to be called (see `_related_section`).
        path_lookup[path.split("/", 1)[-1]] = {"title": title, "html_name": html_name}
        entry = {
            "title": title,
            "domain": domain,
            # `path` is what the served /search endpoint returns its hits as, so the client can
            # map an FTS5 result back to the page it rendered.
            "path": path,
            "html_path": html_name,
            "excerpt": _excerpt(content),
            "tags": tags,
            "doc_type": doc_type,
            "slug": Path(path).stem,
        }
        if stale_days:
            entry["stale"] = 1  # absent, not false, when fresh — this ships on every page load
        if body_cap is not None:
            entry["body"] = _search_body(content, body_cap)
        search_entries.append(entry)

    feature_count = sum(1 for d in docs if d["doc_type"] == "feature")
    workflow_count = sum(1 for d in docs if d["doc_type"] == "workflow")
    tag_chips = [{"name": t, "cls": _tag_class(t)} for t in sorted(all_tags)]

    _write_assets(site_dir, search_entries, hover_map)
    if body_cap is None:
        print(
            f"specky render-html: {len(docs)} docs is past the in-page search budget, so the "
            "viewer's own search box matches titles and excerpts only. Run `specky serve` and "
            "browse over http:// for full-text search against the index.",
        )
    stale_count = sum(1 for d in docs if d["stale_days"])
    rail_html = _render_rail(
        domains, tag_chips, feature_count > 0, workflow_count > 0, stale_count > 0
    )

    any_mermaid_source = False
    any_mermaid_rendered = False
    for doc in docs:
        body_html, has_source, has_rendered = render_doc_body(
            doc["content"], glossary, doc["doc_type"]
        )
        any_mermaid_source = any_mermaid_source or has_source
        any_mermaid_rendered = any_mermaid_rendered or has_rendered
        body = _doc_header(doc) + body_html + _related_section(doc, path_lookup)
        page = _page(doc["title"], rail_html, body, doc["doc_type"])
        (site_dir / doc["html_name"]).write_text(page)

    if any_mermaid_source and not any_mermaid_rendered:
        print(
            "specky render-html: docs contain ```mermaid``` diagrams but none could be "
            "rendered (fenced source left as-is). Run `specky setup-diagrams` once, then "
            "re-render.",
        )

    home_body = _home_body(len(docs), len(domains), feature_count, workflow_count, tag_chips)
    home_page = _page("specky docs", rail_html, home_body)
    (site_dir / "index.html").write_text(home_page)

    return site_dir / "index.html"
