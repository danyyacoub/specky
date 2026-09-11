"""Static HTML doc site: a searchable, file://-browsable view of the specs/ tree for
non-technical readers. No build step and no external requests (fonts/JS fall back to
system stacks) — every page is self-contained, so the whole site is just files you can
open directly or zip up and send someone.

The look is adapted from Glia's design system (packages/design-system in that repo):
a single blue accent rather than a busy palette, a neutral gray ramp capped at a dark
gray (never pure black), and a primary-tinted "rail" for navigation against a paper-white
content card — depth from soft shadows, not from color.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import markdown as md
from jinja2 import Environment

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

_PAGE_TEMPLATE = _env.from_string(
    '<!doctype html><html><head><meta charset="utf-8">'
    "<title>{{ title }}</title><style>{{ css | safe }}</style></head>"
    '<body><div class="shell">{{ rail | safe }}'
    '<div class="main"><div class="card">{{ body | safe }}</div></div>'
    "</div><script>const SPECKY_INDEX = {{ search_json | safe }};\n{{ search_js | safe }}"
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
.card table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 0.8125rem; }
.card th, .card td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--neutral-200); }
.card th { color: var(--neutral-600); font-weight: 600; font-size: 0.6875rem; text-transform: uppercase; letter-spacing: 0.05em; }
.card blockquote { border-left: 3px solid var(--primary-100); margin: 0; padding: 4px 16px; color: var(--neutral-600); }
.stat-row { display: flex; gap: 12px; margin: 20px 0 0; }
.stat { background: var(--primary-50); border-radius: var(--radius-md); padding: 12px 16px; }
.stat .n { font-size: 1.375rem; font-weight: 600; color: var(--primary); display: block; }
.stat .label { font-size: 0.6875rem; color: var(--neutral-600); }
.empty-state { color: var(--neutral-600); }
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


def _render_rail(domains: dict[str, list[dict]], active_html_name: str | None) -> str:
    ordered = [
        (domain, sorted(domains[domain], key=lambda d: d["title"]))
        for domain in sorted(domains, key=_domain_sort_key)
    ]
    return _RAIL_TEMPLATE.render(domains=ordered, active=active_html_name)


def _page(title: str, rail_html: str, body_html: str, search_json: str) -> str:
    return _PAGE_TEMPLATE.render(
        title=title, css=CSS, rail=rail_html, body=body_html, search_js=SEARCH_JS, search_json=search_json
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

    for html_name, domain, title, path, content in docs:
        body = (
            f'<div class="breadcrumb">{domain}</div>'
            + md.markdown(content, extensions=["tables", "fenced_code"])
        )
        page = _page(title, _render_rail(domains, html_name), body, search_json)
        (site_dir / html_name).write_text(page)

    home_body = (
        "<h1>specky docs</h1>"
        "<p>Auto-generated, browsable functional reference — no server required.</p>"
        f'<div class="stat-row">'
        f'<div class="stat"><span class="n">{len(docs)}</span><span class="label">docs</span></div>'
        f'<div class="stat"><span class="n">{len(domains)}</span><span class="label">domains</span></div>'
        "</div>"
        '<p style="margin-top:24px" class="empty-state">Pick a doc from the left, or search above.</p>'
    )
    home_page = _page("specky docs", _render_rail(domains, None), home_body, search_json)
    (site_dir / "index.html").write_text(home_page)
    (site_dir / "search_index.json").write_text(json.dumps(search_entries, indent=2))

    return site_dir / "index.html"
