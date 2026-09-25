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
import posixpath
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

import markdown as md
from jinja2 import Environment

from specky import activity, diagram_render, paths
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
<symbol id="icon-chat" viewBox="0 0 20 20"><rect x="3" y="4" width="14" height="10" rx="2.5"/><polyline points="7,14 7,17 10.5,14"/><polygon points="10,6.3 10.8,8.2 12.7,9 10.8,9.8 10,11.7 9.2,9.8 7.3,9 9.2,8.2"/></symbol>
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
<symbol id="icon-sidebar" viewBox="0 0 20 20"><rect x="3" y="4" width="14" height="12" rx="1.5"/><line x1="8" y1="4" x2="8" y2="16"/></symbol>
<symbol id="icon-x" viewBox="0 0 20 20"><line x1="5.5" y1="5.5" x2="14.5" y2="14.5"/><line x1="14.5" y1="5.5" x2="5.5" y2="14.5"/></symbol>
<symbol id="icon-plus" viewBox="0 0 20 20"><line x1="10" y1="4.5" x2="10" y2="15.5"/><line x1="4.5" y1="10" x2="15.5" y2="10"/></symbol>
<symbol id="icon-arrow-up" viewBox="0 0 20 20"><line x1="10" y1="16" x2="10" y2="4.5"/><polyline points="5,9.5 10,4.5 15,9.5"/></symbol>
<symbol id="icon-copy" viewBox="0 0 20 20"><rect x="7" y="7" width="10" height="10" rx="1.5"/><polyline points="13,4.5 13,3 3,3 3,13 4.5,13"/></symbol>
<symbol id="icon-check" viewBox="0 0 20 20"><polyline points="4,10.5 8,14.5 16,5.5"/></symbol>
<symbol id="icon-stop" viewBox="0 0 20 20"><rect x="5.5" y="5.5" width="9" height="9" rx="1.5"/></symbol>
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
    '<span class="chat-title"><span class="assistant-mark" aria-hidden="true"><svg class="icon">'
    '<use href="#icon-chat"></use></svg></span>'
    '<span class="chat-title-text"><span class="chat-title-name">Spec Assistant</span>'
    '<span id="assistant-subtitle" class="chat-subtitle"></span></span></span>'
    '<span class="chat-header-right">'
    '<button id="chat-reset" class="chat-header-button" type="button" title="New conversation">'
    '<svg class="icon" aria-hidden="true"><use href="#icon-plus"></use></svg>New</button>'
    '<button id="assistant-close" class="chat-header-button icon-only" type="button" '
    'aria-label="Close panel" title="Close (Esc)">'
    '<svg class="icon" aria-hidden="true"><use href="#icon-x"></use></svg></button></span></div>'
    # The log and the thinking overlay share a stage: the overlay floats over the log's bottom edge,
    # right above the input, where the reader's eyes already are after sending.
    '<div class="chat-stage">'
    # Shown only while the log is empty (see CHAT_JS): a first-time reader gets a way in.
    '<div id="chat-empty" class="chat-empty" hidden>'
    '<span class="assistant-mark large" aria-hidden="true"><svg class="icon">'
    '<use href="#icon-chat"></use></svg></span>'
    '<p class="chat-empty-title">Ask the docs, or draft a change</p>'
    '<p class="chat-empty-hint">Answers cite the docs they come from. Type @ to scope a question.</p>'
    '<div id="chat-suggestions" class="chat-suggestions"></div></div>'
    '<div id="chat-log" class="chat-log"></div>'
    '<div id="chat-thinking" class="chat-thinking" role="status" aria-live="polite" hidden>'
    '<span class="thinking-orb" aria-hidden="true"></span>'
    '<span id="chat-status" class="thinking-text"></span>'
    '<span id="chat-elapsed" class="thinking-elapsed" aria-hidden="true"></span>'
    '<button id="chat-stop" class="chat-stop" type="button">'
    '<svg class="icon" aria-hidden="true"><use href="#icon-stop"></use></svg>Stop</button>'
    "</div></div>"
    # Shown while a draft is waiting on the reader: what they type next answers its question or
    # corrects its current step, rather than starting a new conversation (see DRAFT_JS).
    '<div id="draft-reply" class="draft-reply" hidden><span>Replying to the draft</span>'
    '<button id="draft-cancel" class="draft-cancel" type="button">Cancel draft</button></div>'
    '<form id="chat-form" class="chat-form"><div class="chat-composer">'
    '<div class="chat-input-wrap">'
    '<div id="mention-dropdown" class="mention-dropdown" role="listbox" '
    'aria-label="Scope to a module or feature"></div>'
    '<textarea id="chat-input" rows="1" placeholder="Ask about the docs, or describe a change to '
    'draft…" aria-label="Message the Spec Assistant" autocomplete="off"></textarea>'
    "</div>"
    '<div class="chat-composer-bar">'
    # Auto is the default and the honest one: the server classifies the question. The other two
    # exist for when it reads a question the other way round (see chat_server.classify_intent).
    '<div class="assistant-intent" role="group" aria-label="Answer style">'
    '<button class="chip intent-chip" type="button" data-intent="auto" data-active="true" '
    'aria-pressed="true">Auto</button>'
    '<button class="chip intent-chip" type="button" data-intent="explore" data-active="false" '
    'aria-pressed="false">Explore</button>'
    '<button class="chip intent-chip" type="button" data-intent="spec" data-active="false" '
    'aria-pressed="false">Draft spec</button></div>'
    '<button class="chat-send" type="submit" aria-label="Send" disabled>'
    '<svg class="icon" aria-hidden="true"><use href="#icon-arrow-up"></use></svg></button>'
    "</div></div>"
    '<p class="chat-hint"><kbd>Enter</kbd> send · <kbd>Shift</kbd>+<kbd>Enter</kbd> new line · '
    "<kbd>@</kbd> scope</p>"
    "</form></div></aside>"
)

# The shortcut hint starts hidden and empty: CHAT_JS fills in ⌘ or Ctrl for this platform.
_ASSISTANT_TOGGLE = (
    '<button id="chat-toggle" class="chat-toggle" type="button" aria-keyshortcuts="Meta+. Control+.">'
    '<svg class="icon" aria-hidden="true"><use href="#icon-chat"></use></svg>Spec Assistant'
    '<kbd class="chat-kbd" hidden></kbd></button>'
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
  /* The Spec Assistant's identity — launcher, header mark, thinking orb — and nothing else: its
     buttons and links stay the plain accent, so blue still means "clickable" across the viewer.
     White text holds AA contrast on both ends. */
  --assistant-from: #0166ff;
  --assistant-to: #7c3aed;
  --assistant-glow: rgb(98 60 240 / 0.32);
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
    --assistant-from: #2563eb;
    --assistant-glow: rgb(124 92 255 / 0.4);
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
   see diagram_render.py). Falls back to a plain solid bar on engines without backdrop-filter.
   The glass sits on ::before, not on the bar: an element with backdrop-filter is the backdrop
   root for everything inside it, so #search-results — which hangs out of the bar over the page —
   would blur only the bar's own layer and show the page through it, sharp. */
.titlebar {
  position: fixed; top: 0; left: 0; right: 0; z-index: 30;
  display: flex; align-items: center; gap: 20px; height: 52px; padding: 0 18px;
  border-bottom: 1px solid var(--chrome-border);
}
.titlebar::before {
  content: ""; position: absolute; inset: 0; z-index: -1;
  background: var(--chrome-bg); backdrop-filter: blur(20px) saturate(160%);
  -webkit-backdrop-filter: blur(20px) saturate(160%);
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
/* A `#section` link scrolls the content pane, which runs under the fixed titlebar. */
.doc :is(h1, h2, h3, h4, h5, h6)[id] { scroll-margin-top: 64px; }
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
.related .why { color: var(--text-secondary); font-size: 0.8125rem; margin-left: 6px; }

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

/* --- home page: recent activity, one collapsed row per person (activity.py). */
.activity { margin-top: 28px; }
.doc .activity h2 {
  font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-secondary);
  margin: 0 0 4px; border: none; padding: 0;
}
.doc .activity h2::before { content: none; }
.activity-meta, .activity-foot { font-size: 0.75rem; color: var(--text-tertiary); margin: 0 0 12px; }
.activity-foot { margin-top: 12px; }
.activity .chip-row { display: inline-flex; gap: 4px; vertical-align: middle; }
.doc .activity a.chip { text-decoration: none; padding: 2px 8px; }
.person { border: 1px solid var(--border); border-radius: var(--radius-md); margin-bottom: 8px; background: var(--surface); }
.person > summary {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 10px 14px; cursor: pointer;
  list-style: none;
}
.person > summary::-webkit-details-marker { display: none; }
.person > summary:hover { background: var(--surface-secondary); border-radius: var(--radius-md); }
.person[open] > summary { border-bottom: 1px solid var(--border); border-radius: var(--radius-md) var(--radius-md) 0 0; }
.avatar {
  display: inline-grid; place-items: center; width: 28px; height: 28px; border-radius: 50%;
  font-size: 0.6875rem; font-weight: 700; flex-shrink: 0;
}
.person-name { font-weight: 600; }
.person-counts { font-size: 0.75rem; color: var(--text-secondary); }
.person-when { margin-left: auto; font-size: 0.75rem; color: var(--text-tertiary); }
.person-body { padding: 4px 14px 12px; }
.doc .person-body h3 {
  font-size: 0.6875rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-tertiary);
  margin: 14px 0 6px;
}
.changes { list-style: none; margin: 0; padding: 0; }
.change {
  display: grid; grid-template-columns: 52px 1fr; gap: 10px; padding: 8px 0;
  border-top: 1px solid var(--border);
}
.changes > .change:first-child { border-top: none; }
.change > time { font-size: 0.75rem; color: var(--text-tertiary); padding-top: 2px; }
.brief { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
.brief li { font-size: 0.875rem; line-height: 1.45; }
.doc .brief a { color: var(--text-primary); text-decoration: none; }
.doc .brief a:hover { color: var(--accent); text-decoration: underline; }
.undocumented { color: var(--text-secondary); font-style: italic; }
.impact {
  display: inline-block; font-size: 0.625rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.04em; border-radius: 4px; padding: 1px 5px; margin-right: 6px; vertical-align: 1px;
}
.impact-feature { background: var(--feature-bg); color: var(--feature); }
.impact-improvement { background: var(--accent-soft); color: var(--accent); }
.impact-fix { background: var(--tag-2-bg); color: var(--tag-2); }
.internal, .change-meta { font-size: 0.75rem; color: var(--text-secondary); margin: 4px 0 0; }
.change-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; }
.change-meta.branch { margin: 0 0 4px; }
.change-meta code, .brief code { font-size: 0.75rem; }
.activity details.more > summary {
  font-size: 0.75rem; color: var(--accent); cursor: pointer; margin: 4px 0; list-style: none;
}
.activity details.more > summary::-webkit-details-marker { display: none; }
.activity details.more[open] > summary { display: none; }

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

/* --- the launcher: the assistant's one piece of fixed chrome, and the first place its gradient
   shows. The shortcut hint inside it is filled in by CHAT_JS for this platform. */
.chat-toggle {
  position: fixed; bottom: 24px; right: 24px; z-index: 20; display: inline-flex; align-items: center; gap: 8px;
  background: linear-gradient(135deg, var(--assistant-from), var(--assistant-to)); color: #fff;
  border: none; border-radius: 999px; padding: 10px 14px 10px 16px;
  font-family: var(--font-sans); font-size: 0.8125rem; font-weight: 600; letter-spacing: -0.005em;
  box-shadow: 0 8px 24px -8px var(--assistant-glow), 0 2px 6px -2px rgb(0 0 0 / 0.18),
    inset 0 1px 0 rgb(255 255 255 / 0.18);
  cursor: pointer; transition: transform 150ms ease, box-shadow 150ms ease;
}
.chat-toggle .icon { width: 1.15em; height: 1.15em; }
.chat-toggle:hover {
  transform: translateY(-1px);
  box-shadow: 0 14px 30px -8px var(--assistant-glow), 0 3px 8px -2px rgb(0 0 0 / 0.2),
    inset 0 1px 0 rgb(255 255 255 / 0.18);
}
.chat-toggle:active { transform: translateY(0) scale(0.98); }
.chat-toggle:focus-visible { outline-offset: 3px; border-radius: 999px; }
.chat-kbd {
  font-family: var(--font-sans); font-size: 0.6875rem; font-weight: 600; line-height: 1;
  padding: 3px 6px; border-radius: 6px; background: rgb(255 255 255 / 0.2); color: #fff;
}
/* No keyboard to press it on. */
@media (hover: none) { .chat-kbd { display: none; } }
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
  border-left: 1px solid var(--glass-border);
  display: none;
}
/* Glass on ::before, as on .titlebar: on the panel itself it would make the panel the backdrop
   root, and the @ picker and thinking pill floating over the log would show it through sharp. */
.assistant-panel::before {
  content: ""; position: absolute; inset: 0; z-index: -1;
  background: var(--glass-bg); backdrop-filter: blur(24px) saturate(180%);
  -webkit-backdrop-filter: blur(24px) saturate(180%);
}
body.assistant-open .assistant-panel { display: block; }
body.assistant-open .chat-toggle { display: none; }
/* Only when the reader opens it (see openAssistant) — a panel restored on the next page is already
   where they left it, and sliding it in again on every cited source they follow would be noise. */
.assistant-panel.is-entering { animation: assistant-in 200ms cubic-bezier(0.2, 0.8, 0.2, 1); }
@keyframes assistant-in {
  from { opacity: 0; transform: translateX(16px); }
}
.assistant-body { display: flex; flex-direction: column; height: 100%; padding-top: 52px; }
/* The grab area stays 7px wide; what shows is a hairline that thickens to the accent on hover. */
.assistant-resize {
  position: absolute; top: 0; bottom: 0; left: -3px; width: 7px; z-index: 2; cursor: col-resize;
}
.assistant-resize::after {
  content: ""; position: absolute; top: 0; bottom: 0; left: 3px; width: 1px; background: transparent;
  transition: background 120ms ease, width 120ms ease, left 120ms ease;
}
.assistant-resize:hover::after, .assistant-resize:focus-visible::after {
  left: 2px; width: 3px; background: var(--accent);
}
.assistant-resize:focus-visible { outline: none; }
.chat-header {
  padding: 10px 10px 10px 16px; border-bottom: 1px solid var(--border);
  display: flex; justify-content: space-between; align-items: center; gap: 8px;
}
.chat-title { display: flex; align-items: center; gap: 10px; min-width: 0; }
.assistant-mark {
  display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0;
  width: 28px; height: 28px; border-radius: 8px; color: #fff;
  background: linear-gradient(135deg, var(--assistant-from), var(--assistant-to));
  box-shadow: 0 4px 12px -4px var(--assistant-glow), inset 0 1px 0 rgb(255 255 255 / 0.2);
}
.assistant-mark .icon { width: 16px; height: 16px; }
.assistant-mark.large { width: 44px; height: 44px; border-radius: 12px; }
.assistant-mark.large .icon { width: 24px; height: 24px; }
.chat-title-text { display: flex; flex-direction: column; min-width: 0; line-height: 1.3; }
.chat-title-name {
  font-family: var(--font-display); font-weight: 600; font-size: 0.875rem; letter-spacing: -0.01em;
}
.chat-subtitle { font-size: 0.6875rem; color: var(--text-tertiary); }
.chat-subtitle:empty { display: none; }
.chat-header-right { display: flex; align-items: center; gap: 2px; }
.chat-header-button {
  display: inline-flex; align-items: center; gap: 5px; border: none; background: none;
  color: var(--text-secondary); font-family: var(--font-sans); font-size: 0.75rem; font-weight: 500;
  padding: 5px 8px; border-radius: var(--radius-sm); cursor: pointer;
}
.chat-header-button.icon-only { padding: 5px; }
.chat-header-button .icon { width: 1.15em; height: 1.15em; }
.chat-header-button:hover { background: var(--surface-tertiary); color: var(--text-primary); }
/* Narrow windows have no width to give: the dock overlays the content instead of crushing the
   doc column to an unreadable ribbon. */
@media (max-width: 1100px) {
  .assistant-panel {
    position: fixed; top: 0; right: 0; bottom: 0; z-index: 26; flex: none; max-width: none;
    width: min(var(--assistant-width, 560px), 100vw); box-shadow: var(--shadow-md);
  }
}
/* --- the log and the thinking overlay share a stage, so the overlay can float over the log's
   bottom edge — right above the input, where the reader's eyes already are after sending. */
.chat-stage { position: relative; flex: 1; min-height: 0; display: flex; flex-direction: column; }
.chat-empty {
  position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 6px; padding: 24px; text-align: center;
}
.chat-empty[hidden] { display: none; }
.chat-empty .assistant-mark { margin-bottom: 8px; }
.chat-empty-title {
  margin: 0; font-family: var(--font-display); font-size: 1rem; font-weight: 600; letter-spacing: -0.01em;
}
.chat-empty-hint { margin: 0 0 12px; max-width: 36ch; color: var(--text-secondary); font-size: 0.75rem; }
.chat-suggestions { display: flex; flex-direction: column; gap: 6px; width: 100%; max-width: 380px; }
.chat-suggestion {
  display: block; width: 100%; text-align: left; border: 1px solid var(--border); background: var(--surface);
  color: var(--text-primary); font-family: var(--font-sans); font-size: 0.75rem; padding: 8px 12px;
  border-radius: 10px; cursor: pointer; transition: border-color 120ms ease, background 120ms ease;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.chat-suggestion:hover { border-color: var(--accent); background: var(--accent-soft); }
.chat-log {
  flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 10px;
  min-height: 120px; min-width: 0;
}
/* Room under the last message for the overlay, so it never sits on top of what was just said. */
.chat-stage:has(> .chat-thinking:not([hidden])) .chat-log { padding-bottom: 64px; }
.chat-log > * { animation: chat-in 160ms ease-out both; }
@keyframes chat-in {
  from { opacity: 0; transform: translateY(4px); }
}
.chat-msg {
  font-size: 0.8125rem; line-height: 1.55; max-width: 90%; min-width: 0; white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.chat-user {
  align-self: flex-end; background: var(--accent-soft); color: var(--text-primary);
  padding: 8px 12px; border-radius: 16px 16px 4px 16px;
}
.chat-assistant { align-self: flex-start; color: var(--text-primary); }
.chat-sources { align-self: flex-start; color: var(--text-secondary); font-size: 0.6875rem; }
.chat-error {
  align-self: stretch; max-width: 100%; color: var(--danger); background: var(--danger-bg);
  border: 1px solid color-mix(in srgb, var(--danger) 25%, transparent); border-radius: 10px;
  padding: 8px 12px; font-size: 0.75rem; white-space: normal;
}
.chat-error code, .chat-note code {
  font-family: var(--font-mono); font-size: 0.9em; padding: 1px 4px; border-radius: 4px;
  background: color-mix(in srgb, currentColor 10%, transparent);
}
/* A note is a divider in the conversation ("Stopped.", "Draft cancelled."), not something said. */
.chat-note {
  align-self: stretch; max-width: 100%; display: flex; align-items: center; gap: 10px;
  color: var(--text-tertiary); font-size: 0.6875rem; white-space: normal; text-align: center;
}
.chat-note::before, .chat-note::after { content: ""; flex: 1; min-width: 16px; height: 1px; background: var(--border); }
/* --- the composer: the input with its options right under it, so what the answer will be
   (Auto / Explore / Draft spec) is decided where the question is typed, not at the top of the panel. */
.chat-form { padding: 6px 16px 10px; }
.chat-composer {
  display: flex; flex-direction: column; gap: 6px; padding: 8px 8px 8px 10px;
  border: 1px solid var(--border); border-radius: 16px; background: var(--surface);
  box-shadow: 0 1px 2px rgb(0 0 0 / 0.04), 0 6px 18px -10px rgb(0 0 0 / 0.14);
  transition: border-color 120ms ease, box-shadow 120ms ease;
}
.chat-composer:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.chat-input-wrap { position: relative; }
/* The composer shows focus for the input, so the input itself draws no box or outline. It grows with
   what's typed (see CHAT_JS) up to max-height, then scrolls. */
.chat-form textarea {
  display: block; width: 100%; min-height: 28px; max-height: 180px; margin: 0; padding: 4px;
  border: none; outline: none; background: transparent; resize: none; overflow-y: auto;
  font-family: var(--font-sans); font-size: 0.8125rem; line-height: 1.5; color: var(--text-primary);
  box-sizing: border-box;
}
.chat-form textarea::placeholder { color: var(--text-tertiary); }
.chat-composer-bar { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
/* The answer style is one choice of three, so it reads as a segmented control, not as filters. */
.assistant-intent {
  display: inline-flex; flex-wrap: wrap; gap: 2px; padding: 2px; border-radius: 999px;
  background: var(--surface-tertiary);
}
.intent-chip {
  background: transparent; color: var(--text-secondary); padding: 4px 10px;
  transition: background 120ms ease, color 120ms ease, box-shadow 120ms ease;
}
.intent-chip:hover { color: var(--text-primary); }
.intent-chip[data-active="true"] {
  background: var(--surface); color: var(--text-primary);
  box-shadow: 0 1px 2px rgb(0 0 0 / 0.12), 0 0 0 1px var(--border);
}
.chat-send {
  display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0;
  width: 32px; height: 32px; padding: 0; border: none; border-radius: 50%;
  background: var(--accent); color: var(--accent-fg); cursor: pointer;
  transition: opacity 120ms ease, transform 120ms ease;
}
.chat-send .icon { width: 16px; height: 16px; stroke-width: 2; }
.chat-send:hover:not(:disabled) { transform: translateY(-1px); }
.chat-send:disabled { opacity: 0.35; cursor: default; }
.chat-hint { margin: 6px 4px 0; font-size: 0.625rem; color: var(--text-tertiary); }
.chat-hint kbd {
  font-family: var(--font-sans); font-size: 0.625rem; padding: 0 4px; border: 1px solid var(--border);
  border-bottom-width: 2px; border-radius: 4px; background: var(--surface-secondary); color: var(--text-secondary);
}
/* --- the thinking overlay: a glass pill floating over the bottom of the log while the server works.
   The orb spins, the status shimmers, the clock counts, and Stop gives up on the wait. DRAFT_STATUS
   names the draft step in the same spot. */
.chat-thinking {
  position: absolute; left: 50%; bottom: 12px; z-index: 3; transform: translateX(-50%);
  display: inline-flex; align-items: center; gap: 10px; max-width: calc(100% - 32px);
  padding: 5px 5px 5px 12px; border-radius: 999px; white-space: nowrap;
  background: var(--glass-bg); backdrop-filter: blur(16px) saturate(180%);
  -webkit-backdrop-filter: blur(16px) saturate(180%); border: 1px solid var(--border);
  box-shadow: var(--shadow-md), 0 0 28px -10px var(--assistant-glow);
  font-size: 0.75rem; font-weight: 500;
  animation: thinking-in 180ms ease-out;
}
.chat-thinking[hidden] { display: none; }
@keyframes thinking-in {
  from { opacity: 0; transform: translate(-50%, 6px); }
}
.thinking-orb {
  width: 16px; height: 16px; flex-shrink: 0; border-radius: 50%;
  background: conic-gradient(from 0deg, transparent 0deg, var(--assistant-from) 110deg,
    var(--assistant-to) 300deg, transparent 360deg);
  -webkit-mask: radial-gradient(farthest-side, transparent calc(100% - 3px), #000 calc(100% - 2.5px));
  mask: radial-gradient(farthest-side, transparent calc(100% - 3px), #000 calc(100% - 2.5px));
  animation: thinking-spin 0.9s linear infinite;
}
@keyframes thinking-spin {
  to { transform: rotate(1turn); }
}
.thinking-text {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; color: var(--text-secondary);
  background: linear-gradient(90deg, var(--text-secondary) 35%, var(--assistant-to) 50%, var(--text-secondary) 65%);
  background-size: 250% 100%; -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent;
  animation: thinking-shimmer 1.8s linear infinite;
}
@keyframes thinking-shimmer {
  from { background-position: 100% 0; }
  to { background-position: 0% 0; }
}
.thinking-elapsed { color: var(--text-tertiary); font-size: 0.6875rem; font-variant-numeric: tabular-nums; }
.thinking-elapsed:empty { display: none; }
.chat-stop {
  display: inline-flex; align-items: center; gap: 4px; border: none; border-radius: 999px;
  background: var(--surface-tertiary); color: var(--text-primary); font-family: var(--font-sans);
  font-size: 0.6875rem; font-weight: 600; padding: 4px 10px 4px 8px; cursor: pointer;
}
.chat-stop .icon { width: 0.95em; height: 0.95em; fill: currentColor; }
.chat-stop:hover { background: var(--border); }
@media (prefers-reduced-motion: reduce) {
  .thinking-orb { animation: none; }
  .thinking-text { animation: none; background: none; -webkit-text-fill-color: currentColor; }
  .assistant-panel.is-entering, .chat-log > *, .chat-thinking { animation: none; }
  .chat-toggle, .chat-send, .chat-composer, .intent-chip { transition: none; }
  .chat-toggle:hover, .chat-toggle:active, .chat-send:hover:not(:disabled) { transform: none; }
}
/* --- the @ picker: arrow keys move .active, Enter or Tab takes it (see MENTION_JS). */
.mention-dropdown {
  position: absolute; bottom: calc(100% + 12px); left: -10px; right: -8px; z-index: 25;
  background: var(--glass-bg); backdrop-filter: blur(20px) saturate(160%);
  -webkit-backdrop-filter: blur(20px) saturate(160%); border: 1px solid var(--glass-border);
  border-radius: var(--radius-lg); box-shadow: var(--shadow-md); padding: 4px;
  max-height: 240px; overflow-y: auto;
}
.mention-dropdown:empty { display: none; border: none; box-shadow: none; }
.mention-label {
  padding: 4px 8px 2px; font-size: 0.625rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--text-tertiary);
}
.mention-dropdown .mention-item {
  display: flex; width: 100%; align-items: center; gap: 8px; padding: 6px 8px; border: none;
  border-radius: var(--radius-md); background: none; text-align: left; cursor: pointer;
  font-family: var(--font-sans); font-size: 0.75rem; color: var(--text-primary);
}
.mention-dropdown .mention-item.active, .mention-dropdown .mention-item:focus-visible {
  background: var(--accent-soft);
}
.mention-item .icon { color: var(--text-tertiary); }
.mention-item[data-type="feature"] .icon { color: var(--feature); }
.mention-item[data-type="workflow"] .icon { color: var(--workflow); }
.mention-item .label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mention-item .kind { color: var(--text-tertiary); font-size: 0.6875rem; flex-shrink: 0; }
/* --- a rendered answer: the panel's version of `.doc`, not a chat bubble. Full width of the
   log (a table or diagram has nowhere to go in a 90% bubble), normal wrapping (the markdown is
   real HTML now, not preformatted text), and the doc page's own figure/table/diagram styling
   reused as-is — the same server-side pipeline produced both. */
.chat-rich {
  align-self: stretch; max-width: 100%; white-space: normal; background: none; padding: 2px 0;
  font-size: 0.8125rem; line-height: 1.6;
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
/* break-word, not the anywhere .chat-msg sets: anywhere makes every letter a break point when auto
   table layout measures a column's minimum width, so short columns were squeezed until "Resolve
   range" read "Resolv / e range". With whole words as the minimum, a table too wide for the panel
   scrolls inside its figure instead of splitting them. */
.chat-rich figure.tw {
  margin: 10px 0; overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius-md);
  overflow-wrap: break-word;
}
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

/* --- under an answer: a quiet toolbar (style badge, Copy, the cited docs as pills). */
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
  display: inline-flex; align-items: center; gap: 4px; border: none; background: none;
  color: var(--text-secondary); font-family: var(--font-sans); font-size: 0.6875rem; font-weight: 500;
  padding: 3px 6px; border-radius: var(--radius-sm); cursor: pointer;
}
.chat-copy:hover { background: var(--surface-tertiary); color: var(--text-primary); }
.chat-sources-label { margin-left: 2px; color: var(--text-tertiary); }
.chat-source {
  display: inline-flex; align-items: center; gap: 4px; max-width: 100%; padding: 2px 8px 2px 6px;
  border: 1px solid var(--border); border-radius: 999px; background: var(--surface);
  color: var(--text-secondary); text-decoration: none; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis;
}
.chat-source .icon { color: var(--text-tertiary); }
a.chat-source { color: var(--accent); }
a.chat-source:hover { border-color: var(--accent); background: var(--accent-soft); }
/* --- Explore: the short answer shows, the details wait behind "Read more". */
.chat-details[hidden] { display: none; }
.chat-details { border-top: 1px dashed var(--border); margin-top: 8px; padding-top: 4px; }
.chat-more {
  align-self: flex-start; display: inline-flex; align-items: center; gap: 6px; border: none;
  background: none; color: var(--accent); font-family: var(--font-sans); font-size: 0.75rem;
  font-weight: 600; padding: 0; cursor: pointer;
}
/* A CSS chevron, so the JS can keep setting the label with textContent. */
.chat-more::after {
  content: ""; width: 5px; height: 5px; border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor; transform: translateY(-2px) rotate(45deg);
  transition: transform 150ms ease;
}
.chat-more[aria-expanded="true"]::after { transform: translateY(1px) rotate(-135deg); }
.chat-more:hover { text-decoration: underline; }
/* --- a draft step: built from structured data in the browser (DRAFT_JS), styled like an answer. */
.draft-card {
  border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 12px 14px;
  background: var(--surface);
}
.draft-steps { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; font-size: 0.625rem; }
.draft-step {
  border-radius: 999px; padding: 2px 8px; border: 1px solid var(--border); color: var(--text-tertiary);
  font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em;
}
.draft-step[data-state="done"] { border-color: transparent; background: var(--feature-bg); color: var(--feature); opacity: 0.7; }
.draft-step[data-state="active"] {
  border-color: currentColor; background: var(--feature-bg); color: var(--feature);
}
.draft-target { font-size: 0.75rem; color: var(--text-secondary); margin: 0 0 6px; }
.draft-target code { font-size: 0.6875rem; }
.draft-target .draft-kind {
  border-radius: 999px; padding: 1px 6px; margin-left: 4px; font-size: 0.625rem; font-weight: 600;
  background: var(--accent-soft); color: var(--accent);
}
.chat-rich .draft-card-title { margin: 10px 0 4px; font-size: 0.8125rem; }
.draft-options, .draft-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.draft-action {
  border: 1px solid var(--border); background: var(--surface); color: var(--text-primary);
  font-family: var(--font-sans); font-size: 0.75rem; font-weight: 500; padding: 6px 12px;
  border-radius: var(--radius-md); cursor: pointer; transition: background 120ms ease, border-color 120ms ease;
}
.draft-action:hover:not(:disabled) { background: var(--surface-tertiary); }
.draft-action.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-fg); }
.draft-action.primary:hover:not(:disabled) { background: var(--accent); filter: brightness(1.08); }
.draft-action:disabled { opacity: 0.45; cursor: default; }
.draft-hint { color: var(--text-tertiary); font-size: 0.6875rem; margin: 8px 0 0; }
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
  margin: 0 16px 4px; padding: 6px 6px 6px 12px; border-radius: 10px; font-size: 0.6875rem;
  font-weight: 600; color: var(--feature); background: var(--feature-bg);
}
.draft-reply[hidden] { display: none; }
.draft-reply > span::before {
  content: ""; display: inline-block; width: 6px; height: 6px; margin-right: 8px; border-radius: 50%;
  background: currentColor; vertical-align: 1px;
}
.draft-cancel {
  border: none; background: none; color: var(--text-secondary); font-family: var(--font-sans);
  font-size: 0.6875rem; font-weight: 500; padding: 3px 8px; border-radius: var(--radius-sm); cursor: pointer;
}
.draft-cancel:hover { color: var(--text-primary); background: var(--surface); }
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
    // The reader pressing Stop says nothing about where the API lives, so it mustn't move the base.
    if (speckyApiSettled || err.name === 'AbortError') throw err;
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
const chatElapsed = document.getElementById('chat-elapsed');
const chatStop = document.getElementById('chat-stop');
const chatSend = chatForm?.querySelector('.chat-send');
const chatReset = document.getElementById('chat-reset');
const chatEmpty = document.getElementById('chat-empty');
const chatSuggestions = document.getElementById('chat-suggestions');
const intentChips = [...document.querySelectorAll('.intent-chip')];
const CHAT_LOG_MAX = 24;
const CHAT_PERSISTED = new Set(['user', 'assistant', 'sources']);
const ASSISTANT_WIDTH_MIN = 320;
const ASSISTANT_WIDTH_MAX = 1200;
const ASSISTANT_DOC_MIN = 360;
const ASSISTANT_OFFLINE = 'The Spec Assistant is not reachable. Run `specky serve` in this repo, then try again.';
const CHAT_INPUT_MAX = 180;

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

function isAssistantOpen() {
  return document.body.classList.contains('assistant-open');
}

// Opening the panel hands it the nav rail's width; closing gives back whatever the rail was before.
// Only the reader's own open/close does this — reopening on the next page leaves the rail as the
// reader last set it, so one who brought the nav back mid-conversation keeps it. Likewise only this
// open slides the panel in; a restored one is already where the reader left it.
function openAssistant() {
  tabStore?.setItem('specky-nav-before-assistant', isNavCollapsed() ? '1' : '0');
  setNavCollapsed(true);
  assistantPanel?.classList.add('is-entering');
  setAssistantOpen(true);
}

function closeAssistant() {
  setAssistantOpen(false);
  setNavCollapsed(tabStore?.getItem('specky-nav-before-assistant') === '1');
  chatToggle?.focus();
}

chatToggle?.addEventListener('click', openAssistant);
assistantClose?.addEventListener('click', closeAssistant);
assistantPanel?.addEventListener('animationend', (event) => {
  if (event.target === assistantPanel) assistantPanel.classList.remove('is-entering');
});
if (tabStore?.getItem('specky-assistant-open') === '1') setAssistantOpen(true);

// ⌘. / Ctrl+. toggles the panel from anywhere on the page; Esc closes it from inside. The @ picker
// claims Esc first (MENTION_JS marks it handled), so Esc there closes only the picker.
const ASSISTANT_ON_MAC = /mac|iphone|ipad/i.test(navigator.userAgentData?.platform || navigator.platform || '');
const assistantKbd = chatToggle?.querySelector('.chat-kbd');
if (assistantKbd) {
  assistantKbd.textContent = ASSISTANT_ON_MAC ? '⌘ .' : 'Ctrl .';
  assistantKbd.hidden = false;
}
document.addEventListener('keydown', (event) => {
  if (event.defaultPrevented) return;
  if ((event.metaKey || event.ctrlKey) && event.key === '.') {
    event.preventDefault();
    if (isAssistantOpen()) closeAssistant();
    else openAssistant();
  } else if (event.key === 'Escape' && isAssistantOpen() && assistantPanel?.contains(document.activeElement)) {
    event.preventDefault();
    closeAssistant();
  }
});

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
    const active = chip.dataset.intent === value;
    chip.dataset.active = active ? 'true' : 'false';
    chip.setAttribute('aria-pressed', active ? 'true' : 'false');
  }
}
setAssistantIntent(assistantIntent);
for (const chip of intentChips) {
  chip.addEventListener('click', () => setAssistantIntent(chip.dataset.intent));
}

// The thinking overlay shows only while there's a status to show; an empty one hides it. Its clock
// runs for the whole wait: a draft moving from one step's status to the next keeps counting.
let thinkingClock = null;

function setChatStatus(text) {
  if (chatStatus) chatStatus.textContent = text;
  if (chatThinking) chatThinking.hidden = !text;
  if (text && !thinkingClock) {
    const started = Date.now();
    if (chatElapsed) chatElapsed.textContent = '';
    thinkingClock = setInterval(() => {
      const seconds = Math.floor((Date.now() - started) / 1000);
      if (chatElapsed) chatElapsed.textContent = seconds ? `${seconds}s` : '';
    }, 1000);
    if (chatLog) chatLog.scrollTop = chatLog.scrollHeight;
  } else if (!text && thinkingClock) {
    clearInterval(thinkingClock);
    thinkingClock = null;
  }
}

// One request at a time is in flight, the /chat question or a draft step; Stop gives up on it. The
// server isn't told — a stopped /chat answer still joins the conversation it keeps for this session.
let chatAbort = null;

function beginChatRequest() {
  chatAbort = new AbortController();
  return chatAbort.signal;
}

function isStopped(err) {
  return err?.name === 'AbortError';
}

chatStop?.addEventListener('click', () => chatAbort?.abort());

function persistChatEntry(role, text) {
  if (!tabStore || !CHAT_PERSISTED.has(role)) return;
  const log = readChatLog();
  log.push({ role, text });
  tabStore.setItem('specky-chat-log', JSON.stringify(log.slice(-CHAT_LOG_MAX)));
}

// `code` spans in an error or a note ("Run `specky serve`…") become <code>, built from text nodes —
// never markup, since an error's text can come from the server.
function textWithCode(node, text) {
  text.split('`').forEach((part, i) => {
    if (!part) return;
    if (i % 2) {
      const code = document.createElement('code');
      code.textContent = part;
      node.appendChild(code);
    } else {
      node.appendChild(document.createTextNode(part));
    }
  });
}

function addChatMessage(role, text, persist = true) {
  const div = document.createElement('div');
  div.className = `chat-msg chat-${role}`;
  if (role === 'note') textWithCode(div.appendChild(document.createElement('span')), text);
  else if (role === 'error') textWithCode(div, text);
  else div.textContent = text;
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
  const pill = document.createElement(hit ? 'a' : 'span');
  pill.className = 'chat-source';
  pill.title = source;
  if (hit) {
    pill.href = hit.html_path;
    pill.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#icon-file-text"></use></svg>';
  }
  pill.appendChild(document.createTextNode(hit ? hit.title : source));
  return pill;
}

// A static icon plus a text label: the markdown being copied never goes near innerHTML.
function setCopyLabel(button, icon, label) {
  button.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#icon-${icon}"></use></svg>`;
  button.appendChild(document.createTextNode(label));
}

function copyMarkdownButton(markdown) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'chat-copy';
  button.title = 'Copy the answer as markdown';
  setCopyLabel(button, 'copy', 'Copy');
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
    setCopyLabel(button, 'check', 'Copied');
    setTimeout(() => setCopyLabel(button, 'copy', 'Copy'), 1500);
  });
  return button;
}

function appendSources(actions, sources) {
  if (!sources.length) return;
  const label = document.createElement('span');
  label.className = 'chat-sources-label';
  label.textContent = 'Sources';
  actions.appendChild(label);
  for (const source of sources) actions.appendChild(sourceLink(source));
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

// --- the composer: a textarea that grows with what's typed, Enter to send, Shift+Enter for a newline.
function syncComposer() {
  if (!chatInput) return;
  chatInput.style.height = 'auto';
  chatInput.style.height = `${Math.min(chatInput.scrollHeight, CHAT_INPUT_MAX)}px`;
  if (chatSend) chatSend.disabled = !chatInput.value.trim();
}

function fillComposer(text) {
  chatInput.value = text;
  syncComposer();
  chatInput.focus();
  chatInput.setSelectionRange(text.length, text.length);
}

chatInput?.addEventListener('input', syncComposer);
chatInput?.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
  // An open @ picker owns Enter: it picks the highlighted item (MENTION_JS).
  if (mentionHits.length) return;
  event.preventDefault();
  chatForm.requestSubmit();
});

chatForm?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const question = chatInput.value.trim();
  if (!question) return;
  closeMentions();
  addChatMessage('user', question);
  chatInput.value = '';
  syncComposer();
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
      signal: beginChatRequest(),
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
    if (isStopped(err)) addChatMessage('note', 'Stopped.', false);
    else addChatMessage('error', ASSISTANT_OFFLINE);
  } finally {
    chatAbort = null;
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
    addChatMessage('note', 'Earlier answers are replayed as plain text. The next one renders in full.', false);
  }
}

// --- an empty conversation: a welcome and a few ways in, drawn from the page the reader is on --------
// A suggestion only fills the composer. Sending is still the reader's call — each one costs a model call.
function chatSuggestionsFor(page) {
  const doc = SPECKY_INDEX.find((d) => d.html_path === page);
  if (!doc) {
    return ['What are the main modules?', 'Which features does this repo document?', 'Draft a spec for a new feature'];
  }
  const out = [`What does "${doc.title}" cover?`];
  if (doc.doc_type === 'feature' || doc.doc_type === 'workflow') {
    out.push(`@feature:${doc.slug} what are the edge cases?`);
    out.push(`Draft a change to @feature:${doc.slug} — `);
  } else {
    out.push(`@module:${doc.domain} what does this module cover?`);
  }
  return out;
}

for (const text of chatSuggestionsFor(currentPage)) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'chat-suggestion';
  button.textContent = text;
  button.title = text;
  button.addEventListener('click', () => fillComposer(text));
  chatSuggestions?.appendChild(button);
}

// Every path that fills or clears the log (an answer, a draft card, a replay, New) is a child-list
// change, so one observer keeps the welcome in step with all of them.
function syncChatEmpty() {
  if (chatEmpty && chatLog) chatEmpty.hidden = chatLog.childElementCount > 0;
}
if (chatLog) new MutationObserver(syncChatEmpty).observe(chatLog, { childList: true });
syncChatEmpty();

// What the assistant answers from, at a glance. Commit summaries (the history domain) are indexed
// too, but they'd swamp a count of the docs, and root holds the glossary, not a module.
const assistantSubtitle = document.getElementById('assistant-subtitle');
const assistantDocs = SPECKY_INDEX.filter((d) => d.domain !== 'history');
if (assistantSubtitle && assistantDocs.length) {
  const modules = new Set(assistantDocs.map((d) => d.domain).filter((domain) => domain !== 'root')).size;
  assistantSubtitle.textContent = `${assistantDocs.length} docs · ${modules} modules`;
}
syncComposer();
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
      signal: beginChatRequest(),
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
    // A step is stateless on the server (the state travels with each request), so stopping one
    // leaves the draft exactly where it was: the current card's buttons work again.
    if (isStopped(err)) addChatMessage('note', 'Stopped.', false);
    else addChatMessage('error', ASSISTANT_OFFLINE);
    reviveLatestDraftCard();
  } finally {
    draftBusy = false;
    chatAbort = null;
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

# '@' mention autocomplete for the chat input: candidates come straight from SPECKY_INDEX
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
      out.push({ kind: 'module', value: d.domain, label: d.domain, icon: 'folder' });
    }
  }
  for (const d of SPECKY_INDEX) {
    if (d.doc_type === 'feature' || d.doc_type === 'workflow') {
      out.push({
        kind: 'feature', value: d.slug, label: d.title, type: d.doc_type,
        icon: TYPE_ICONS[d.doc_type] || TYPE_ICON_FALLBACK,
      });
    }
  }
  return out;
})();

let mentionMatchStart = -1;
let mentionMatchEnd = -1;
let mentionHits = [];
let mentionActive = 0;

function currentMentionQuery() {
  const caret = chatInput.selectionStart ?? chatInput.value.length;
  const upToCaret = chatInput.value.slice(0, caret);
  // Only an '@' that starts a word opens the picker, so typing an email address doesn't.
  const match = /(^|\\s)@([\\w-]*)$/.exec(upToCaret);
  if (!match) return null;
  return { query: match[2].toLowerCase(), start: match.index + match[1].length, end: caret };
}

function closeMentions() {
  mentionDropdown.replaceChildren();
  mentionHits = [];
}

// Built from text nodes: a label is a doc title, and a doc title is the repo's text, not markup.
function renderMentionDropdown(candidates) {
  closeMentions();
  mentionHits = candidates.slice(0, 8);
  mentionActive = 0;
  if (!mentionHits.length) return;
  const label = document.createElement('div');
  label.className = 'mention-label';
  label.textContent = 'Scope to…';
  mentionDropdown.appendChild(label);
  mentionHits.forEach((c, i) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'mention-item';
    btn.id = `mention-${i}`;
    btn.tabIndex = -1;
    btn.setAttribute('role', 'option');
    if (c.type) btn.dataset.type = c.type;
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('class', 'icon');
    icon.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#icon-${c.icon}`);
    icon.appendChild(use);
    const name = document.createElement('span');
    name.className = 'label';
    name.textContent = c.label;
    const kind = document.createElement('span');
    kind.className = 'kind';
    kind.textContent = c.kind;
    btn.append(icon, name, kind);
    btn.addEventListener('mousedown', (event) => event.preventDefault());  // keep the caret in the input
    // mousemove, not mouseenter: a list that opens under a resting pointer would otherwise steal the
    // highlight from the arrow keys before the reader has touched the mouse.
    btn.addEventListener('mousemove', () => { if (mentionActive !== i) setMentionActive(i); });
    btn.addEventListener('click', () => selectMention(c));
    mentionDropdown.appendChild(btn);
  });
  setMentionActive(0);
}

function setMentionActive(index) {
  mentionActive = (index + mentionHits.length) % mentionHits.length;
  mentionDropdown.querySelectorAll('.mention-item').forEach((btn, i) => {
    const active = i === mentionActive;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
    if (active) btn.scrollIntoView({ block: 'nearest' });
  });
}

function selectMention(candidate) {
  const token = `@${candidate.kind}:${candidate.value} `;
  const value = chatInput.value;
  chatInput.value = value.slice(0, mentionMatchStart) + token + value.slice(mentionMatchEnd);
  closeMentions();
  chatInput.focus();
  const caret = mentionMatchStart + token.length;
  chatInput.setSelectionRange(caret, caret);
  syncComposer();
}

chatInput?.addEventListener('input', () => {
  const state = currentMentionQuery();
  if (!state) {
    closeMentions();
    mentionMatchStart = -1;
    mentionMatchEnd = -1;
    return;
  }
  mentionMatchStart = state.start;
  mentionMatchEnd = state.end;
  const hits = MENTION_CANDIDATES.filter((c) => c.label.toLowerCase().includes(state.query) || c.value.toLowerCase().includes(state.query));
  renderMentionDropdown(hits);
});

// Arrow keys move the highlight; Enter or Tab takes the highlighted item, not always the first.
// Handled keys are marked defaultPrevented, which is how Esc here closes only the picker and not
// the panel behind it (see CHAT_JS).
chatInput?.addEventListener('keydown', (event) => {
  if (!mentionHits.length) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    closeMentions();
  } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    setMentionActive(mentionActive + (event.key === 'ArrowDown' ? 1 : -1));
  } else if (event.key === 'Enter' || event.key === 'Tab') {
    event.preventDefault();
    selectMention(mentionHits[mentionActive]);
  }
});
chatInput?.addEventListener('blur', () => setTimeout(closeMentions, 150));
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


def page_name(doc_path: str) -> str:
    """The viewer page a doc is rendered to. Anything linking to a page (a chat answer's cited
    doc, say) names it through this, so the link can't drift from the file `render_site` writes."""
    return f"{slug(doc_path)}.html"


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
    return {
        term: _strip_markdown(text)
        for term, text in paths.read_term_table(paths.glossary(repo_root)).items()
    }


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


# --- in-body links and heading anchors ---------------------------------------------------
# A doc links another the way it reads in the repo — `[check](../cli/check.md)`, relative to
# itself — but a rendered site has no `.md` files and no folders: every page is a flat
# `<slug>.html`. So each renderer resolves those links to wherever *it* put the target (a page
# here, a section of the one file in `specky export`). Both steps run after `render_doc_body`,
# never inside it: `_STEP_SECTION` matches a bare `<h2>`, and an `id` on it would switch the
# workflow stepper off.

# Links never nest, so the lazy label can't run on into a second one.
_LINK = re.compile(r"<a\b(?P<attrs>[^>]*)>(?P<label>.*?)</a>", re.DOTALL)
_HREF_ATTR = re.compile(r'\bhref="(?P<href>[^"]*)"')
_URL_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
_HEADING = re.compile(r"<h(?P<level>[1-6])>(?P<text>.*?)</h(?P=level)>", re.DOTALL)

# `(target, anchor)` → the href to use, or None when this output has no such thing. `target` is
# the repo-relative path the link meant; `anchor` is the part after `#`, "" when there was none.
HrefFor = Callable[[str, str], str | None]


def rewrite_links(fragment: str, doc_path: str, href_for: HrefFor) -> str:
    """Every relative link in `fragment` resolved through `href_for`.

    A link is resolved against `doc_path`, the repo-relative path of the doc it sits in, so
    `../cli/check.md` in `specs/catalog/feature-graph.md` becomes `specs/cli/check.md`; a bare
    `#section` gets `doc_path` itself. A link `href_for` has no answer for is unwrapped to its
    label: the same words without a link read as prose, while a dead link reads as a broken site.
    Absolute URLs, `mailto:` and root-relative paths are left exactly as written.
    """

    def resolve(match: re.Match[str]) -> str:
        attrs = match.group("attrs")
        found = _HREF_ATTR.search(attrs)
        if found is None:
            return match.group(0)
        href = html.unescape(found.group("href"))
        if not href or href.startswith("/") or _URL_SCHEME.match(href):
            return match.group(0)
        path, _, anchor = href.partition("#")
        target = (
            posixpath.normpath(posixpath.join(posixpath.dirname(doc_path), unquote(path)))
            if path
            else doc_path
        )
        new_href = href_for(target, anchor)
        if new_href is None:
            return match.group("label")
        new_attrs = (
            attrs[: found.start()]
            + f'href="{html.escape(new_href, quote=True)}"'
            + attrs[found.end() :]
        )
        return f"<a{new_attrs}>{match.group('label')}</a>"

    return _LINK.sub(resolve, fragment)


def _heading_id(text: str) -> str:
    """GitHub's anchor for a heading, because that's what an author checks their `#links`
    against: lowercased, punctuation dropped, each space a hyphen — "Scope, And What It Doesn't
    Hide" is `scope-and-what-it-doesnt-hide`."""
    plain = html.unescape(_TAGS.sub("", text)).strip().lower()
    return re.sub(r"[^\w\- ]", "", plain).replace(" ", "-")


def anchor_headings(fragment: str, prefix: str = "") -> str:
    """An `id` on every heading, so a `#section` link has somewhere to land.

    A repeated heading gets `-1`, `-2`, … as on GitHub. `prefix` is for a page holding more than
    one doc (`specky export`), where every doc's `## Edge Cases` would otherwise share one id.
    """
    seen: dict[str, int] = {}

    def add_id(match: re.Match[str]) -> str:
        level, text = match.group("level"), match.group("text")
        base = _heading_id(text)
        if not base:
            return match.group(0)
        count = seen.get(base, 0)
        seen[base] = count + 1
        anchor = f"{base}-{count}" if count else base
        return f'<h{level} id="{html.escape(prefix + anchor, quote=True)}">{text}</h{level}>'

    return _HEADING.sub(add_id, fragment)


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


# How many tag siblings a page lists under Related. Past a handful the list stops being "the docs to
# read next" and becomes the tag's index, which the rail's tag filter already is.
RELATED_BY_TAG_LIMIT = 6


def _related_section(doc: dict, path_lookup: dict[str, dict], docs: list[dict] = ()) -> str:
    """The docs to read next: hand-written `related:` links first, then the docs sharing a tag.

    The tag siblings are derived here, at render time, rather than written into anyone's
    frontmatter. That was the other way to get cross-doc links, and it's the worse one: `related:`
    is reserved for a link no shared tag explains (the `document-domain` skill says so), an agent
    writing one doc can't safely edit thirty others, and a derived list can't go stale — it's
    recomputed from the tags on every render, the same way `specky graph` draws its edges. Ordered
    by how many tags a sibling shares, then title, and each names the tags it shares so the reader
    sees *why* it's listed.
    """
    items = []
    listed = {doc["path"]}
    for slug in doc["related"]:
        target = path_lookup.get(f"{slug}.md")
        if not target:
            continue
        listed.add(target["path"])
        items.append(
            f'<li><a href="{target["html_name"]}">'
            f'<svg class="icon" aria-hidden="true"><use href="#icon-link"></use></svg>'
            f'{html.escape(target["title"])}</a></li>'
        )
    own = set(doc["tags"])
    siblings = sorted(
        (
            (sorted(own & set(other["tags"])), other)
            for other in docs
            if other["path"] not in listed and other["doc_type"] and own & set(other["tags"])
        ),
        key=lambda pair: (-len(pair[0]), pair[1]["title"].lower()),
    )
    for shared, other in siblings[:RELATED_BY_TAG_LIMIT]:
        items.append(
            f'<li><a href="{other["html_name"]}">'
            f'<svg class="icon" aria-hidden="true"><use href="#icon-link"></use></svg>'
            f'{html.escape(other["title"])}</a>'
            f'<span class="why">{html.escape(", ".join(shared))}</span></li>'
        )
    if not items:
        return ""
    return f'<div class="related"><h2>Related</h2><ul>{"".join(items)}</ul></div>'


def _page_href(doc_path: str, pages: dict[str, str]) -> HrefFor:
    """`rewrite_links`' resolver for one viewer page: a doc is its page, a section of this doc is
    a bare `#anchor`, and anything the site didn't render (a file outside the docs tree, a doc
    that no longer exists) is no link at all."""

    def href_for(target: str, anchor: str) -> str | None:
        if target == doc_path and anchor:
            return f"#{anchor}"
        page = pages.get(target)
        if page is None:
            return None
        return f"{page}#{anchor}" if anchor else page

    return href_for


# How much of the activity brief is visible before a "+N more" disclosure: people's lists stay
# scannable, and a merge of forty commits doesn't push everyone else off the screen.
ACTIVITY_ITEMS_SHOWN = 15
ACTIVITY_LINES_SHOWN = 3
ACTIVITY_CHIPS_SHOWN = 4
ACTIVITY_PERSON_CHIPS = 3


_CODE_SPAN = re.compile(r"`([^`]+)`")


def _inline(text: str) -> str:
    """A brief line as HTML: escaped, with the markdown a model writes into one sentence — code
    spans and bold — rendered or dropped rather than shown as backticks and asterisks."""
    return _CODE_SPAN.sub(r"<code>\1</code>", html.escape(text.replace("**", "")))


def _short_date(when: datetime) -> str:
    return f"{when.day} {when:%b}"


def _more(items: list[str], shown: int, tag: str, cls: str) -> str:
    """The first `shown` items, and the rest behind a native disclosure — no script needed."""
    head = "".join(items[:shown])
    if len(items) <= shown:
        return f'<{tag} class="{cls}">{head}</{tag}>'
    rest = "".join(items[shown:])
    return (
        f'<{tag} class="{cls}">{head}</{tag}>'
        f'<details class="more"><summary>+{len(items) - shown} more</summary>'
        f'<{tag} class="{cls}">{rest}</{tag}></details>'
    )


def _activity_chips(
    features: list[str], doc_info: dict[str, dict], shown: int, overflow: bool = True
) -> str:
    known = [doc_info[f] for f in features if f in doc_info]
    chips = "".join(
        f'<a class="chip{" type-" + d["doc_type"] if d["doc_type"] else ""}" '
        f'href="{html.escape(d["html_name"], quote=True)}">{html.escape(d["label"])}</a>'
        for d in known[:shown]
    )
    if overflow and len(known) > shown:
        chips += f'<span class="chip">+{len(known) - shown}</span>'
    return f'<span class="chip-row">{chips}</span>' if chips else ""


def _activity_lines(lines: list[activity.Line], pages: dict[str, str]) -> str:
    """A change's sentences: behaviour first, `internal` ones counted rather than listed."""
    items = []
    for line in [line for line in lines if line.impact != "internal"]:
        text = _inline(line.text)
        page = pages.get(line.history_path)
        hover = f' title="{html.escape(line.detail, quote=True)}"' if line.detail else ""
        if page:
            text = f'<a href="{html.escape(page, quote=True)}"{hover}>{text}</a>'
        elif not line.history_path:
            text = f'<span class="undocumented" title="No history doc for this commit">{text}</span>'
        badge = (
            f'<span class="impact impact-{line.impact}">{line.impact}</span>' if line.impact else ""
        )
        items.append(f"<li>{badge}{text}</li>")
    internal = sum(1 for line in lines if line.impact == "internal")
    html_out = _more(items, ACTIVITY_LINES_SHOWN, "ul", "brief") if items else ""
    if internal:
        html_out += f'<p class="internal">+{internal} internal</p>'
    return html_out


def _activity_html(
    recent: activity.Activity, pages: dict[str, str], doc_info: dict[str, dict]
) -> str:
    """The home page's "Recent activity" section (see activity.py for what goes in it)."""
    meta = (
        f'<p class="activity-meta">on <code>{html.escape(recent.branch)}</code> · last '
        f"{recent.days} days · as of {_short_date(recent.as_of)} {recent.as_of:%Y}</p>"
    )
    if recent.shallow:
        body = (
            '<p class="empty-state">This checkout has shallow history, so who changed what can\'t '
            "be read from it. Fetch the full history (<code>git fetch --unshallow</code>, or "
            "<code>fetch-depth: 0</code> in CI) and render again.</p>"
        )
    elif not recent.people:
        body = (
            f'<p class="empty-state">Nothing landed on <code>{html.escape(recent.branch)}</code> '
            f"in the last {recent.days} days.</p>"
        )
    else:
        body = "".join(_person_html(p, pages, doc_info) for p in recent.people)
    notes = []
    if recent.automated:
        notes.append(f"{recent.automated} automated change(s) by bots or agents not shown")
    if recent.undocumented:
        notes.append(
            f"{recent.undocumented} commit(s) shown by their message, with no history doc yet — "
            "<code>specky sync</code> writes them"
        )
    foot = f'<p class="activity-foot">{" · ".join(notes)}</p>' if notes else ""
    return f'<section class="activity"><h2>Recent activity</h2>{meta}{body}{foot}</section>'


def _change_item(
    when: datetime,
    facts: list[str],
    lines_html: str,
    chips_html: str,
    meta_first: bool = False,
    meta_cls: str = "change-meta",
) -> str:
    """One entry of a person's list: its date, its lines, and a muted line of facts and chips —
    after the lines for a shipped change, before them for a branch, whose name is what it's known by."""
    meta = f'<p class="{meta_cls}">{" · ".join(f for f in facts if f)}{chips_html}</p>'
    body = meta + lines_html if meta_first else lines_html + meta
    return f'<li class="change"><time>{_short_date(when)}</time><div>{body}</div></li>'


def _person_html(
    person: activity.Person, pages: dict[str, str], doc_info: dict[str, dict]
) -> str:
    initials = "".join(w[0] for w in person.name.split()[:2]).upper() or "?"
    tint = _tag_class(person.name).removeprefix("tag-")
    counts = []
    if person.shipped:
        counts.append(f"{len(person.shipped)} shipped")
    if person.in_progress:
        counts.append(f"{len(person.in_progress)} in progress")
    summary = (
        f'<summary><span class="avatar" style="color:var(--tag-{tint});'
        f'background:var(--tag-{tint}-bg)" aria-hidden="true">{html.escape(initials)}</span>'
        f'<span class="person-name">{html.escape(person.name)}</span>'
        f'<span class="person-counts">{" · ".join(counts)}</span>'
        # Their most-touched docs, as a hint of what they work on — not a count of everything.
        f"{_activity_chips(person.features(), doc_info, ACTIVITY_PERSON_CHIPS, overflow=False)}"
        f'<time class="person-when">{_short_date(person.last_active)}</time></summary>'
    )

    def others(names: list[str]) -> str:
        rest = [n for n in names if n != person.name]
        return f"with {html.escape(', '.join(rest))}" if rest else ""

    sections = []
    if person.shipped:
        items = []
        for change in person.shipped:
            label = html.escape(change.label)
            if change.pr_url and label:
                label = f'<a href="{html.escape(change.pr_url, quote=True)}">{label}</a>'
            items.append(
                _change_item(
                    change.date,
                    [
                        label,
                        f"{change.commits} commits" if change.commits > 1 else "",
                        others(change.people),
                    ],
                    _activity_lines(change.lines, pages),
                    _activity_chips(change.features, doc_info, ACTIVITY_CHIPS_SHOWN),
                )
            )
        sections.append("<h3>Shipped</h3>" + _more(items, ACTIVITY_ITEMS_SHOWN, "ul", "changes"))
    if person.in_progress:
        items = [
            _change_item(
                branch.date,
                [
                    f"<code>{html.escape(branch.ref)}</code>",
                    f"{branch.commits} commit{'s' if branch.commits != 1 else ''}",
                    others(branch.people),
                ],
                _activity_lines(branch.lines, pages),
                _activity_chips(branch.features, doc_info, ACTIVITY_CHIPS_SHOWN),
                meta_first=True,
                meta_cls="change-meta branch",
            )
            for branch in person.in_progress
        ]
        sections.append("<h3>In progress</h3>" + _more(items, ACTIVITY_ITEMS_SHOWN, "ul", "changes"))
    return f'<details class="person">{summary}<div class="person-body">{"".join(sections)}</div></details>'


def _home_body(
    doc_count: int,
    domain_count: int,
    feature_count: int,
    workflow_count: int,
    tag_chips: list[dict],
    activity_html: str = "",
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
        f"{activity_html}"
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
            # "history" is the changelog trail, one entry per branch or commit — collapsed
            # by default so it doesn't crowd out the rest of the nav. NAV_JS re-opens the
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


def _render_activity(repo_root: Path, pages: dict[str, str], doc_info: dict[str, dict]) -> str:
    """The activity section, or "" — never a reason for the rest of the site not to render."""
    try:
        found = activity.collect(repo_root)
    except ValueError as exc:
        print(f"specky render-html: recent activity left out — {exc}")
        return ""
    except subprocess.CalledProcessError as exc:
        print(f"specky render-html: recent activity left out — git failed: {exc.stderr.strip()}")
        return ""
    return _activity_html(found, pages, doc_info) if found else ""


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
    # Full repo path → page, for links inside doc bodies (`rewrite_links`), which name their
    # target by path rather than by `related:` slug.
    pages: dict[str, str] = {}
    all_tags: set[str] = set()
    for row in rows:
        path, domain, title, content, doc_type, tags_raw, related_raw, owner = row[:8]
        stale_since, last_code = row[8:]
        html_name = page_name(path)
        tags = [t for t in tags_raw.split(",") if t]
        related = [r for r in related_raw.split(",") if r]
        all_tags.update(tags)
        # 0 for a doc staleness.py didn't flag, so every consumer can treat this as "days behind"
        # and stay uninterested in which of the two columns was empty.
        stale_days = days_behind(stale_since, last_code) if stale_since and last_code else 0
        doc = {
            "path": path,
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
        path_lookup[path.split("/", 1)[-1]] = {"title": title, "html_name": html_name, "path": path}
        pages[path] = html_name
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
        body_html = anchor_headings(body_html)
        body_html = rewrite_links(body_html, doc["path"], _page_href(doc["path"], pages))
        body = _doc_header(doc) + body_html + _related_section(doc, path_lookup, docs)
        page = _page(doc["title"], rail_html, body, doc["doc_type"])
        (site_dir / doc["html_name"]).write_text(page)

    if any_mermaid_source and not any_mermaid_rendered:
        print(
            "specky render-html: docs contain ```mermaid``` diagrams but none could be "
            "rendered (fenced source left as-is). Run `specky setup-diagrams` once, then "
            "re-render.",
        )

    doc_info = {
        d["path"]: {
            "label": _nav_title(d["title"], d["domain"], repo_root.name),
            "html_name": d["html_name"],
            "doc_type": d["doc_type"] if d["doc_type"] in ("feature", "workflow") else "",
        }
        for d in docs
    }
    home_body = _home_body(
        len(docs),
        len(domains),
        feature_count,
        workflow_count,
        tag_chips,
        _render_activity(repo_root, pages, doc_info),
    )
    home_page = _page("specky docs", rail_html, home_body)
    (site_dir / "index.html").write_text(home_page)

    return site_dir / "index.html"
