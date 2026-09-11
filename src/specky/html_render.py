"""Static HTML doc site: a searchable, file://-browsable view of the specs/ tree for
non-technical readers. No build step for the *reader* (fonts/JS fall back to system
stacks) — every page is self-contained, so the whole site is just files you can open
directly or zip up and send someone. Search is fully static (the index is inlined, not
fetched). One thing reaches outside a page itself: the optional chat widget, which POSTs
to a local `specky serve` companion on 127.0.0.1 (see chat_server.py) — browsing and
search work identically whether or not that server is running.

A ```mermaid``` fence in a doc is rendered to a plain static `<svg>` at `render-html`
time (via `vendor/mermaid-render/`, a Node tool wrapping `beautiful-mermaid` — see that
directory's README) rather than shipping a diagram-rendering library to every reader.
If Node or that tool's dependencies aren't set up, the fenced source is left as
plain-text fallback rather than failing the whole render — same as any doc with no
diagrams at all.

Glossary terms from `specs/GLOSSARY.md` are auto-linked to a hover tooltip on first
mention per page, and tables get a scrollable, zebra-striped treatment — both patterns
(and the diagram approach above) ported from the user's `glia` project
(`apps/backend/app/domains/docs/enrichment_render.py`), adapted from that project's
sandboxed-iframe-hosted pages to specky's plain self-contained ones.

The look is adapted from Glia's design system (packages/design-system in that repo):
a single blue accent rather than a busy palette, a neutral gray ramp capped at a dark
gray (never pure black), and a primary-tinted "rail" for navigation against a paper-white
content card — depth from soft shadows, not from color.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import shutil
from pathlib import Path

import markdown as md
from jinja2 import Environment

from specky.chat_server import DEFAULT_PORT as CHAT_PORT
from specky.db import connect

_env = Environment(autoescape=True)

_RAIL_TEMPLATE = _env.from_string(
    '<div class="rail">'
    '<a class="brand" href="index.html">specky docs</a>'
    '<p class="tagline">Functional reference, kept in sync automatically.</p>'
    '<input id="search-input" class="search-box" placeholder="Search docs…" autocomplete="off">'
    '<div id="search-results"></div>'
    "{% for domain, docs in domains %}"
    '<div class="domain-group"><h2>{{ domain }}</h2><ul>'
    "{% for doc in docs %}"
    '<li><a class="{{ "active" if doc.html_name == active else "" }}" '
    'href="{{ doc.html_name }}">{{ doc.title }}</a></li>'
    "{% endfor %}</ul></div>"
    "{% endfor %}</div>"
)

_CHAT_WIDGET = (
    '<button id="chat-toggle" class="chat-toggle">Ask</button>'
    '<div id="chat-panel" class="chat-panel">'
    '<div class="chat-header">Ask about these docs<span id="chat-status"></span></div>'
    '<div id="chat-log" class="chat-log"></div>'
    '<form id="chat-form" class="chat-form">'
    '<input id="chat-input" placeholder="Ask a question…" autocomplete="off">'
    "<button type=\"submit\">Send</button></form></div>"
)

_PAGE_TEMPLATE = _env.from_string(
    '<!doctype html><html><head><meta charset="utf-8">'
    "<title>{{ title }}</title><style>{{ css | safe }}</style></head>"
    '<body><div class="shell">{{ rail | safe }}'
    '<div class="main"><div class="card">{{ body | safe }}</div></div>'
    "</div>" + _CHAT_WIDGET + "{{ glossary_island | safe }}"
    "<script>const SPECKY_INDEX = {{ search_json | safe }};\n"
    "const SPECKY_CHAT_PORT = {{ chat_port }};\n"
    "{{ search_js | safe }}\n{{ chat_js | safe }}\n{{ glossary_js | safe }}"
    "</script></body></html>"
)

CSS = """
:root {
  --primary: #0166ff;
  --primary-50: #e8f1ff;
  --primary-100: #cce0ff;
  --neutral-0: #ffffff;
  --neutral-50: #f7f7f8;
  --neutral-200: #e4e5e8;
  --neutral-600: #52545a;
  --neutral-900: #2b2d31;
  --radius-md: 8px;
  --radius-lg: 12px;
  --shadow-md: 0 6px 16px -4px rgb(0 0 0 / 0.08), 0 2px 6px -2px rgb(0 0 0 / 0.05);
  --font-sans: Poppins, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: var(--font-sans);
  font-size: 0.8125rem;
  line-height: 1.5;
  color: var(--neutral-900);
  background: linear-gradient(180deg, var(--primary-50) 0%, #f5f8ff 45%, var(--neutral-0) 100%);
  min-height: 100vh;
}
.shell { display: flex; min-height: 100vh; }
.rail {
  width: 280px;
  flex-shrink: 0;
  background: var(--primary-50);
  border-right: 1px solid var(--primary-100);
  padding: 24px 16px;
  overflow-y: auto;
}
.rail a.brand { display: block; font-size: 1rem; font-weight: 600; letter-spacing: -0.01em; color: var(--neutral-900); text-decoration: none; margin-bottom: 2px; }
.rail .tagline { font-size: 0.6875rem; color: var(--neutral-600); margin: 0 0 20px; }
.search-box {
  width: 100%; padding: 8px 10px; border: 1px solid var(--primary-100); border-radius: var(--radius-md);
  font-family: var(--font-sans); font-size: 0.75rem; margin-bottom: 8px; background: var(--neutral-0);
}
#search-results .hit { display: block; padding: 6px 8px; border-radius: var(--radius-md); text-decoration: none; color: var(--neutral-900); }
#search-results .hit:hover { background: var(--neutral-0); }
#search-results .hit-domain { color: var(--neutral-600); font-size: 0.6875rem; }
#search-results:not(:empty) { margin-bottom: 16px; }
.domain-group { margin-bottom: 18px; }
.domain-group h2 {
  font-size: 0.6875rem; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--neutral-600); font-weight: 600; margin: 0 0 8px;
}
.domain-group ul { list-style: none; margin: 0; padding: 0; }
.domain-group li { margin-bottom: 2px; }
.domain-group a {
  display: block; padding: 6px 8px; border-radius: var(--radius-md);
  color: var(--neutral-900); text-decoration: none; font-size: 0.8125rem;
}
.domain-group a:hover, .domain-group a.active { background: var(--neutral-0); color: var(--primary); }
.main { flex: 1; padding: 40px; display: flex; justify-content: center; }
.card {
  background: var(--neutral-0); border-radius: var(--radius-lg); box-shadow: var(--shadow-md);
  padding: 40px 48px; max-width: 760px; width: 100%; height: fit-content;
}
.breadcrumb { font-size: 0.6875rem; color: var(--neutral-600); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.08em; }
.card h1 { font-size: 1.625rem; letter-spacing: -0.03em; margin-top: 0; }
.card h2 { font-size: 1.125rem; letter-spacing: -0.01em; margin-top: 32px; border-top: 1px solid var(--neutral-200); padding-top: 20px; }
.card h3 { font-size: 1rem; }
.card a { color: var(--primary); }
.card code { font-family: var(--font-mono); background: var(--neutral-50); padding: 2px 5px; border-radius: 4px; font-size: 0.75rem; }
.card pre { background: var(--neutral-900); color: var(--neutral-50); padding: 16px; border-radius: var(--radius-md); overflow-x: auto; }
.card pre code { background: none; color: inherit; padding: 0; }
.card blockquote { border-left: 3px solid var(--primary-100); margin: 0; padding: 4px 16px; color: var(--neutral-600); }
.stat-row { display: flex; gap: 12px; margin: 20px 0 0; }
.stat { background: var(--primary-50); border-radius: var(--radius-md); padding: 12px 16px; }
.stat .n { font-size: 1.375rem; font-weight: 600; color: var(--primary); display: block; }
.stat .label { font-size: 0.6875rem; color: var(--neutral-600); }
.empty-state { color: var(--neutral-600); }

/* --- tables: ported from glia's figure.tw (enrichment_render.py) — the wrapper scrolls,
   so a wide table keeps its own column sizing instead of being squeezed into the card. */
.card figure.tw {
  margin: 16px 0; overflow-x: auto;
  border: 1px solid var(--neutral-200); border-radius: var(--radius-md);
}
.card figure.tw table { border-collapse: collapse; width: 100%; margin: 0; font-size: 0.8125rem; }
.card figure.tw th, .card figure.tw td {
  padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--neutral-200); vertical-align: top;
}
.card figure.tw thead th {
  background: var(--neutral-50); color: var(--neutral-600); font-weight: 600;
  font-size: 0.6875rem; text-transform: uppercase; letter-spacing: 0.05em;
}
.card figure.tw tbody tr:nth-child(even) { background: var(--neutral-50); }
.card figure.tw tbody tr:last-child td { border-bottom: none; }

/* --- diagrams: a static <svg> from vendor/mermaid-render, wrapped the same way glia
   wraps a rendered flow. A diagram too wide to shrink into the card scrolls (`.fx`)
   instead of being squeezed down to unreadable labels. */
.card figure.flow {
  margin: 20px 0; padding: 16px; background: var(--neutral-50); border-radius: var(--radius-md);
  text-align: center;
}
.card figure.flow svg { max-width: 100%; height: auto; }
.card figure.flow .fx { overflow-x: auto; }
.card figure.flow .fx svg { max-width: none; margin: 0; }

/* --- glossary hover terms: ported from glia's `.gl`/`.tip` (enrichment_render.py) --- */
.card .gl { border-bottom: 1px dotted var(--primary); cursor: help; }
.tip {
  position: absolute; z-index: 30; max-width: 34ch;
  background: var(--neutral-900); color: var(--neutral-0);
  padding: 8px 10px; border-radius: var(--radius-md); font-size: 0.75rem; line-height: 1.45;
  box-shadow: var(--shadow-md); pointer-events: none;
}
.tip[hidden] { display: none; }

.chat-toggle {
  position: fixed; bottom: 24px; right: 24px; z-index: 20;
  background: var(--primary); color: var(--neutral-0); border: none; border-radius: var(--radius-lg);
  padding: 10px 18px; font-family: var(--font-sans); font-size: 0.8125rem; font-weight: 600;
  box-shadow: var(--shadow-md); cursor: pointer;
}
.chat-panel {
  position: fixed; bottom: 76px; right: 24px; z-index: 20; width: 340px; max-height: 460px;
  background: var(--neutral-0); border-radius: var(--radius-lg); box-shadow: var(--shadow-md);
  border: 1px solid var(--neutral-200); display: none; flex-direction: column; overflow: hidden;
}
.chat-panel.open { display: flex; }
.chat-header {
  padding: 12px 16px; font-weight: 600; font-size: 0.8125rem; border-bottom: 1px solid var(--neutral-200);
  display: flex; justify-content: space-between; align-items: center;
}
#chat-status { font-weight: 400; color: var(--neutral-600); font-size: 0.6875rem; }
.chat-log { flex: 1; overflow-y: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 8px; min-height: 120px; }
.chat-msg { font-size: 0.75rem; line-height: 1.5; padding: 6px 10px; border-radius: var(--radius-md); max-width: 90%; white-space: pre-wrap; }
.chat-user { align-self: flex-end; background: var(--primary-50); color: var(--neutral-900); }
.chat-assistant { align-self: flex-start; background: var(--neutral-50); color: var(--neutral-900); }
.chat-sources { align-self: flex-start; color: var(--neutral-600); font-size: 0.6875rem; }
.chat-error { align-self: flex-start; color: #b91c1c; background: #fef2f2; }
.chat-form { display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--neutral-200); }
.chat-form input { flex: 1; padding: 8px 10px; border: 1px solid var(--neutral-200); border-radius: var(--radius-md); font-family: var(--font-sans); font-size: 0.75rem; }
.chat-form button { background: var(--primary); color: var(--neutral-0); border: none; border-radius: var(--radius-md); padding: 8px 14px; font-family: var(--font-sans); font-size: 0.75rem; font-weight: 600; cursor: pointer; }
"""

SEARCH_JS = """
const input = document.getElementById('search-input');
const results = document.getElementById('search-results');
input?.addEventListener('input', () => {
  const q = input.value.trim().toLowerCase();
  results.innerHTML = '';
  if (!q) return;
  const hits = SPECKY_INDEX.filter(d =>
    d.title.toLowerCase().includes(q) || d.domain.toLowerCase().includes(q) || d.excerpt.toLowerCase().includes(q)
  ).slice(0, 15);
  for (const hit of hits) {
    const a = document.createElement('a');
    a.className = 'hit';
    a.href = hit.html_path;
    a.innerHTML = `${hit.title}<div class="hit-domain">${hit.domain}</div>`;
    results.appendChild(a);
  }
});
"""

CHAT_JS = """
const chatToggle = document.getElementById('chat-toggle');
const chatPanel = document.getElementById('chat-panel');
const chatLog = document.getElementById('chat-log');
const chatForm = document.getElementById('chat-form');
const chatInput = document.getElementById('chat-input');
const chatStatus = document.getElementById('chat-status');

chatToggle?.addEventListener('click', () => {
  chatPanel.classList.toggle('open');
  if (chatPanel.classList.contains('open')) chatInput.focus();
});

function addChatMessage(role, text) {
  const div = document.createElement('div');
  div.className = `chat-msg chat-${role}`;
  div.textContent = text;
  chatLog.appendChild(div);
  chatLog.scrollTop = chatLog.scrollHeight;
}

chatForm?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const question = chatInput.value.trim();
  if (!question) return;
  addChatMessage('user', question);
  chatInput.value = '';
  chatStatus.textContent = 'Thinking…';
  try {
    const res = await fetch(`http://127.0.0.1:${SPECKY_CHAT_PORT}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
    });
    const data = await res.json();
    chatStatus.textContent = '';
    if (!res.ok) {
      addChatMessage('error', data.error || 'Something went wrong.');
      return;
    }
    addChatMessage('assistant', data.answer);
    if (data.sources && data.sources.length) {
      addChatMessage('sources', `Sources: ${data.sources.join(', ')}`);
    }
  } catch (err) {
    chatStatus.textContent = '';
    addChatMessage('error', 'Chat server not reachable. Run `specky serve` in this repo, then try again.');
  }
});
"""

# Ported from glia's `_TOOLTIP_SCRIPT` (enrichment_render.py) — same hover, minus the
# host-messaging half glia needs for its sandboxed-iframe pages, which a plain page here
# doesn't. One tooltip element reused for every hover, not one per term.
GLOSSARY_JS = """
const glDataEl = document.getElementById('gl-data');
const GLOSSARY = glDataEl ? JSON.parse(glDataEl.textContent) : {};
const tip = document.createElement('div');
tip.className = 'tip';
tip.setAttribute('role', 'tooltip');
tip.hidden = true;
document.body.appendChild(tip);

function showGlossaryTip(target) {
  const key = (target.dataset.term || '').toLowerCase();
  const entry = GLOSSARY[key];
  if (!entry) return;
  tip.textContent = entry;
  tip.hidden = false;
  const box = target.getBoundingClientRect();
  tip.style.top = (box.bottom + window.scrollY + 6) + 'px';
  tip.style.left = Math.max(8, box.left + window.scrollX) + 'px';
}
function hideGlossaryTip() { tip.hidden = true; }

for (const el of document.querySelectorAll('[data-term]')) {
  el.tabIndex = 0;
  el.addEventListener('mouseenter', () => showGlossaryTip(el));
  el.addEventListener('focus', () => showGlossaryTip(el));
  el.addEventListener('mouseleave', hideGlossaryTip);
  el.addEventListener('blur', hideGlossaryTip);
}
"""

_DOMAIN_ORDER_FIRST = "root"
_DOMAIN_ORDER_LAST = "history"


def _domain_sort_key(domain: str) -> tuple[int, str]:
    if domain == _DOMAIN_ORDER_FIRST:
        return (0, domain)
    if domain == _DOMAIN_ORDER_LAST:
        return (2, domain)
    return (1, domain)


def _slug(domain: str, stem: str) -> str:
    return f"{domain}-{stem}"


def _excerpt(content: str, length: int = 160) -> str:
    text = re.sub(r"^#.*$", "", content, count=1, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[#*`_>|]", "", text)
    text = " ".join(text.split())
    return f"{text[:length]}…" if len(text) > length else text


# --- tables ---------------------------------------------------------------------------

_TABLE = re.compile(r"<table>(.*?)</table>", re.DOTALL)


def _wrap_tables(body_html: str) -> str:
    """Wrap `tables` extension output in a scrollable figure — see `figure.tw` in CSS."""
    return _TABLE.sub(r'<figure class="tw"><table>\1</table></figure>', body_html)


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
    path = repo_root / "specs" / "GLOSSARY.md"
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
        return f'<span class="gl" data-term="{html.escape(word, quote=True)}">{word}</span>'

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


def _glossary_island(hover: dict[str, str]) -> str:
    """The lowercase-keyed hover map, as JSON the page's tooltip script reads."""
    if not hover:
        return ""
    payload = json.dumps(hover, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/json" id="gl-data">{payload}</script>'


# --- diagrams: fenced ```mermaid``` -> static <svg> via vendor/mermaid-render ----------

_VENDOR_DIR = Path(__file__).parent / "vendor"
_MERMAID_TOOL_DIR = _VENDOR_DIR / "mermaid-render"
_MERMAID_BLOCK = re.compile(r'<pre><code class="language-mermaid">(.*?)</code></pre>', re.DOTALL)
_SVG_ROOT_WIDTH = re.compile(r'<svg[^>]*\swidth="([\d.]+)"')
_SVG_IMPORT = re.compile(r"@import[^;]*;")
_SVG_DANGEROUS_TAG = re.compile(r"<(script|foreignObject|iframe)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_SVG_EVENT_ATTR = re.compile(r'\s+on\w+="[^"]*"', re.IGNORECASE)

# Kept in sync by hand with the CSS `:root` block above — colors rarely change, and this
# runs server-side (no live `getComputedStyle` to read them back from, unlike a browser).
MERMAID_THEME = {
    "fg": "#2b2d31",  # --neutral-900
    "line": "#52545a",  # --neutral-600
    "accent": "#0166ff",  # --primary
    "muted": "#52545a",  # --neutral-600
    "surface": "#ffffff",  # --neutral-0
    "border": "#e4e5e8",  # --neutral-200
    "font": "Poppins",  # first family of --font-sans; renderer quotes it as one name
    "transparent": True,
    "padding": 8,
    "nodeSpacing": 16,
    "layerSpacing": 36,
}
# Past this, a diagram gets its own horizontal scroll instead of being shrunk into the
# card. The card's content width is 760 - 2*48 = 664px; below that labels stop being
# readable, so a wide diagram scrolls at full size rather than shrinking.
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

    `None` covers every reason this can't happen — Node missing, `npm install` not run
    in `vendor/mermaid-render/`, or the source itself failing to parse — and the caller's
    response to all of them is the same: leave the fenced source as readable text rather
    than failing the render. See that directory's README for the one-time setup step.
    """
    if not (_MERMAID_TOOL_DIR / "node_modules").exists():
        return None
    payload = json.dumps({"source": source, "options": MERMAID_THEME})
    try:
        proc = subprocess.run(
            ["node", "render.mjs"],
            input=payload,
            capture_output=True,
            text=True,
            cwd=_MERMAID_TOOL_DIR,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return _scrub_svg(proc.stdout)


def _render_mermaid_blocks(body_html: str) -> tuple[str, bool, bool]:
    """Replace fenced mermaid blocks with rendered `<figure class="flow">` SVGs.

    Returns `(html, any_mermaid_source, any_rendered)` — the first two counts drive
    `render_site()`'s one-time hint if diagrams exist but none could be rendered (Node or
    the tool's `node_modules` missing).
    """
    any_source = False
    any_rendered = False

    def repl(match: re.Match[str]) -> str:
        nonlocal any_source, any_rendered
        any_source = True
        svg = render_mermaid_svg(html.unescape(match.group(1)))
        if svg is None:
            return match.group(0)
        any_rendered = True
        width_match = _SVG_ROOT_WIDTH.search(svg)
        wide = width_match is not None and float(width_match.group(1)) > WIDE_DIAGRAM_PX
        inner = f'<div class="fx">{svg}</div>' if wide else svg
        return f'<figure class="flow">{inner}</figure>'

    return _MERMAID_BLOCK.sub(repl, body_html), any_source, any_rendered


def _render_rail(domains: dict[str, list[dict]], active_html_name: str | None) -> str:
    ordered = [
        (domain, sorted(domains[domain], key=lambda d: d["title"]))
        for domain in sorted(domains, key=_domain_sort_key)
    ]
    return _RAIL_TEMPLATE.render(domains=ordered, active=active_html_name)


def _page(
    title: str, rail_html: str, body_html: str, search_json: str, glossary_island: str = ""
) -> str:
    return _PAGE_TEMPLATE.render(
        title=title,
        css=CSS,
        rail=rail_html,
        body=body_html,
        search_js=SEARCH_JS,
        search_json=search_json,
        chat_js=CHAT_JS,
        chat_port=CHAT_PORT,
        glossary_js=GLOSSARY_JS,
        glossary_island=glossary_island,
    )


def render_site(repo_root: Path) -> Path:
    conn = connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT path, domain, title, content FROM documents ORDER BY domain, title"
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
    glossary_island = _glossary_island(hover_map)

    domains: dict[str, list[dict]] = {}
    search_entries = []
    docs = []
    for path, domain, title, content in rows:
        html_name = f"{_slug(domain, Path(path).stem)}.html"
        domains.setdefault(domain, []).append({"title": title, "html_name": html_name})
        docs.append((html_name, domain, title, path, content))
        search_entries.append(
            {"title": title, "domain": domain, "html_path": html_name, "excerpt": _excerpt(content)}
        )

    # Escape "</" so a doc excerpt containing a literal "</script>" (e.g. a commit diff
    # touching frontend code) can't break out of the inline <script> block it's embedded in.
    search_json = json.dumps(search_entries).replace("</", "<\\/")

    any_mermaid_source = False
    any_mermaid_rendered = False
    for html_name, domain, title, path, content in docs:
        body_html = md.markdown(content, extensions=["tables", "fenced_code"])
        body_html = link_glossary(body_html, glossary)
        body_html = _wrap_tables(body_html)
        body_html, has_source, has_rendered = _render_mermaid_blocks(body_html)
        any_mermaid_source = any_mermaid_source or has_source
        any_mermaid_rendered = any_mermaid_rendered or has_rendered
        body = f'<div class="breadcrumb">{domain}</div>' + body_html
        page = _page(title, _render_rail(domains, html_name), body, search_json, glossary_island)
        (site_dir / html_name).write_text(page)

    if any_mermaid_source and not any_mermaid_rendered:
        print(
            "specky render-html: docs contain ```mermaid``` diagrams but none could be "
            "rendered (fenced source left as-is). Run "
            "`npm install --prefix src/specky/vendor/mermaid-render` (from the specky "
            "checkout) once, then re-render.",
        )

    home_body = (
        "<h1>specky docs</h1>"
        "<p>Auto-generated, browsable functional reference — no server required.</p>"
        f'<div class="stat-row">'
        f'<div class="stat"><span class="n">{len(docs)}</span><span class="label">docs</span></div>'
        f'<div class="stat"><span class="n">{len(domains)}</span><span class="label">domains</span></div>'
        "</div>"
        '<p style="margin-top:24px" class="empty-state">Pick a doc from the left, or search above.</p>'
    )
    home_page = _page("specky docs", _render_rail(domains, None), home_body, search_json, glossary_island)
    (site_dir / "index.html").write_text(home_page)
    (site_dir / "search_index.json").write_text(json.dumps(search_entries, indent=2))

    return site_dir / "index.html"
