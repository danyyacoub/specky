"""`specky export` — the docs as one file to hand to someone who will never open the viewer.

The viewer is a folder of pages you browse. That's the wrong shape for the three things people
actually ask for once docs exist: a single file to attach to an email, a PDF for a review packet,
and a page in the company wiki everyone else already reads. This module writes those, reusing
`html_render`'s markdown → glossary → tables → mermaid pipeline (`render_doc_body`) so the export
is the *same* content as the viewer rather than a second renderer's guess at it.

Three shapes, one pass over the index:

- No flag (the default): one `.specky/export/specky-docs.html` with a domain-grouped table of
  contents, every doc inlined, a print stylesheet and **no JavaScript at all**. No-JS is the point:
  it's what makes the file survive being emailed, opened from a network share, or fed to a PDF
  printer. It also means the viewer's glossary tooltips can't come along, so a glossary term
  becomes a native `<abbr title="…">` hover instead of the viewer's scripted span.
- `--pdf`: the same file, run through `weasyprint` if it's installed. If it isn't, the single page
  is still written and the report says where it is and what to install — printing to PDF from a
  browser is a perfectly good answer, and shipping a PDF engine as a hard dependency for a docs
  plugin isn't.
- `--confluence`: one Confluence *storage format* XHTML per doc, plus an index page. Storage format
  is not HTML: it rejects inline `<svg>`, drops unknown classes, and wants fenced code inside an
  `<ac:structured-macro>`. So mermaid diagrams stay as their source in a code macro (Confluence has
  no server-side mermaid), glossary hovers are dropped, and table wrappers are unwrapped.

`specs/history/` is excluded unless `--include-history`: it holds one doc per commit, so on a
1,000-commit repo that's 1,000 per-commit notes ahead of the docs a reader came for — and the
whole value of a single-page export is that someone will read it start to finish. The report always
says how many were left out and how to include them, so nothing is dropped silently.
"""

from __future__ import annotations

import html as html_mod
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from specky.db import connect
from specky.html_render import (
    domain_sort_key,
    load_glossary,
    markdown_html,
    render_doc_body,
    slug,
)

EXPORT_DIR = ".specky/export"
SINGLE_PAGE_NAME = "specky-docs.html"
PDF_NAME = "specky-docs.pdf"
CONFLUENCE_INDEX = "index.xhtml"
HISTORY_PREFIX = "specs/history/"

MODES = ("single-page", "confluence")

# A single page big enough to hurt is a real outcome on a repo with a lot of docs (and certainly
# with --include-history). Nothing is truncated — an export is a deliverable, and a silently
# shortened deliverable is worse than a big one — but the report says so, because a 20 MB
# attachment is a surprise worth having before you send it, not after.
LARGE_EXPORT_BYTES = 5_000_000


@dataclass(frozen=True)
class Doc:
    path: str
    domain: str
    title: str
    content: str
    doc_type: str
    owner: str

    @property
    def anchor(self) -> str:
        return slug(self.path)

    @property
    def name(self) -> str:
        return self.path.removeprefix("specs/")


@dataclass
class Export:
    mode: str
    written: list[Path] = field(default_factory=list)
    doc_count: int = 0
    history_skipped: int = 0
    pdf: Path | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def bytes_written(self) -> int:
        return sum(p.stat().st_size for p in self.written if p.exists())


# --- the docs, from the index ------------------------------------------------------------


def collect(repo_root: Path, include_history: bool = False) -> tuple[list[Doc], int]:
    """Every doc worth exporting, plus how many `specs/history/` docs were left out."""
    conn = connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT path, domain, title, content, doc_type, owner FROM documents"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise RuntimeError("no documents indexed yet — run `specky index` first")

    docs, skipped = [], 0
    for path, domain, title, content, doc_type, owner in rows:
        if path.startswith(HISTORY_PREFIX) and not include_history:
            skipped += 1
            continue
        docs.append(Doc(path, domain, title, content, doc_type or "", owner or ""))
    docs.sort(key=lambda d: (domain_sort_key(d.domain), d.title))
    return docs, skipped


def _grouped(docs: list[Doc]) -> list[tuple[str, list[Doc]]]:
    groups: dict[str, list[Doc]] = {}
    for doc in docs:
        groups.setdefault(doc.domain, []).append(doc)
    return sorted(groups.items(), key=lambda kv: domain_sort_key(kv[0]))


# --- the single page ---------------------------------------------------------------------

# Deliberately not the viewer's CSS: that one is dark-mode-aware, has fixed-position rails sized
# for a window, and assumes a stylesheet loaded next to the page. This is one <style> block for a
# single scrolling column that also prints, which is the whole job.
PRINT_CSS = """
@page { size: A4; margin: 18mm 16mm; }
* { box-sizing: border-box; }
body {
  margin: 0 auto; padding: 32px 28px; max-width: 46rem; background: #fff; color: #1d1f23;
  font: 15px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
}
h1, h2, h3, h4, h5, h6 { line-height: 1.25; margin: 1.6em 0 .5em; font-weight: 620; }
h1 { font-size: 1.9rem; margin-top: 0; }
h2 { font-size: 1.45rem; }
h3 { font-size: 1.15rem; }
h4, h5, h6 { font-size: 1rem; }
p, ul, ol, table, pre, figure { margin: 0 0 1em; }
ul, ol { padding-left: 1.4em; }
a { color: #0166ff; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            font-size: .88em; }
code { background: #f2f3f5; padding: .1em .35em; border-radius: 3px; }
pre { background: #f7f8fa; border: 1px solid #e3e5e9; border-radius: 6px; padding: 12px 14px;
      overflow-x: auto; white-space: pre-wrap; word-break: break-word; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; font-size: .92em; }
th, td { border: 1px solid #e3e5e9; padding: 6px 9px; text-align: left; vertical-align: top; }
th { background: #f2f3f5; font-weight: 600; }
tbody tr:nth-child(even) { background: #fafbfc; }
figure.tw { margin: 0 0 1em; overflow-x: auto; }
figure.flow { margin: 0 0 1em; text-align: center; }
figure.flow svg { max-width: 100%; height: auto; }
abbr[title] { border-bottom: 1px dotted #63666d; text-decoration: none; cursor: help; }
.cover { border-bottom: 2px solid #e3e5e9; padding-bottom: 18px; margin-bottom: 24px; }
.cover p { color: #63666d; margin: .4em 0 0; }
.toc ol { list-style: none; padding-left: 0; }
.toc > ol > li { margin-bottom: 1em; }
.toc .domain { font-weight: 620; text-transform: capitalize; }
.toc ol ol { padding-left: 1em; margin-top: .3em; }
.toc a { text-decoration: none; }
.doc { border-top: 1px solid #e3e5e9; padding-top: 22px; margin-top: 30px; }
.doc-meta { color: #63666d; font-size: .85em; margin: 0 0 1em; }
.doc-meta .sep { padding: 0 .5em; }
@media print {
  body { max-width: none; padding: 0; font-size: 10.5pt; }
  a { color: inherit; text-decoration: none; }
  .doc { break-before: page; border-top: 0; margin-top: 0; padding-top: 0; }
  h1, h2, h3, h4 { break-after: avoid; }
  table, pre, figure { break-inside: avoid; }
  .toc { break-after: page; }
}
"""

_HEADING_TAG = re.compile(r"<(/?)h([1-5])\b")
# `link_glossary`'s output. The wrapped text is always a bare word — it never wraps inside a tag
# (see `_NO_WRAP_INSIDE`) — so one non-greedy match per span is exact, not a guess.
_GLOSSARY_SPAN = re.compile(r'<span class="gl" data-term="([^"]*)">(.*?)</span>', re.DOTALL)
_CODE_BLOCK = re.compile(r'<pre><code(?: class="language-([^"]*)")?>(.*?)</code></pre>', re.DOTALL)
_TABLE_FIGURE = re.compile(r'<figure class="tw">(.*?)</figure>', re.DOTALL)
_ANY_TAG = re.compile(r"<[^>]+>")


def demote_headings(fragment: str) -> str:
    """Push every heading down one level so the page has exactly one `<h1>`: its own title.

    A doc's `# Title` and the export's own title would otherwise both be `h1`, which breaks the
    document outline every screen reader and PDF bookmark list is built from. `h6` stays put —
    there's no `h7` — which only bites a doc nested six levels deep.
    """
    return _HEADING_TAG.sub(lambda m: f"<{m.group(1)}h{int(m.group(2)) + 1}", fragment)


def inline_glossary_titles(fragment: str, glossary: dict[str, str]) -> str:
    """Turn the viewer's scripted glossary spans into `<abbr title="…">`.

    The single page ships no JS, so the tooltip script isn't there to read `data-term`. `abbr` gets
    the same hover from the browser itself, and degrades to plain underlined text in print.
    """
    lookup = {term.lower(): definition for term, definition in glossary.items()}

    def repl(match: re.Match[str]) -> str:
        term = html_mod.unescape(match.group(1))
        definition = lookup.get(term.lower())
        if not definition:
            return match.group(2)
        return f'<abbr title="{html_mod.escape(definition, quote=True)}">{match.group(2)}</abbr>'

    return _GLOSSARY_SPAN.sub(repl, fragment)


_META_SEP = '<span class="sep">·</span>'


def _doc_meta(doc: Doc) -> str:
    bits = [f"<code>{html_mod.escape(doc.name)}</code>"]
    if doc.doc_type:
        bits.append(doc.doc_type.capitalize())
    if doc.owner:
        bits.append(f"Who to ask: <strong>{html_mod.escape(doc.owner)}</strong>")
    return f'<p class="doc-meta">{_META_SEP.join(bits)}</p>'


def _toc(groups: list[tuple[str, list[Doc]]]) -> str:
    items = []
    for domain, docs in groups:
        links = "".join(
            f'<li><a href="#{doc.anchor}">{html_mod.escape(doc.title)}</a></li>' for doc in docs
        )
        items.append(
            f'<li><span class="domain">{html_mod.escape(domain)}</span><ol>{links}</ol></li>'
        )
    return f'<nav class="toc"><h2>Contents</h2><ol>{"".join(items)}</ol></nav>'


def single_page_html(
    docs: list[Doc], glossary: dict[str, str], title: str = "Documentation"
) -> tuple[str, bool, bool]:
    """The whole export as one self-contained HTML string.

    Returns `(html, any mermaid source, any of it rendered)`, same as `render_doc_body`, so the
    caller can print the one-time `specky setup-diagrams` hint the viewer prints.
    """
    groups = _grouped(docs)
    any_source = any_rendered = False
    sections = []
    for _, group in groups:
        for doc in group:
            body, has_source, has_rendered = render_doc_body(doc.content, glossary)
            any_source = any_source or has_source
            any_rendered = any_rendered or has_rendered
            body = inline_glossary_titles(demote_headings(body), glossary)
            sections.append(
                f'<section class="doc" id="{doc.anchor}">{_doc_meta(doc)}{body}</section>'
            )
    page = (
        "<!doctype html>\n"
        f'<html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html_mod.escape(title)}</title><style>{PRINT_CSS}</style></head><body>"
        f'<header class="cover"><h1>{html_mod.escape(title)}</h1>'
        f"<p>{len(docs)} document(s), generated by specky.</p></header>"
        f"{_toc(groups)}{''.join(sections)}"
        "</body></html>\n"
    )
    return page, any_source, any_rendered


# --- Confluence storage format -----------------------------------------------------------


def _code_macro(language: str, source: str) -> str:
    """Confluence's code block. CDATA, not entities: storage format keeps the body verbatim."""
    text = html_mod.unescape(source).rstrip("\n")
    # `]]>` inside the source would end the section early; splitting it across two is the standard
    # escape and reassembles to the same characters.
    text = text.replace("]]>", "]]]]><![CDATA[>")
    param = (
        f'<ac:parameter ac:name="language">{html_mod.escape(language)}</ac:parameter>'
        if language and language != "mermaid"
        else ""
    )
    return (
        f'<ac:structured-macro ac:name="code" ac:schema-version="1">{param}'
        f"<ac:plain-text-body><![CDATA[{text}]]></ac:plain-text-body></ac:structured-macro>"
    )


def confluence_body(content: str) -> str:
    """One doc as Confluence storage-format XHTML.

    Starts from the same markdown step as the viewer, then unwinds the three things the viewer adds
    that storage format won't take: the glossary's `<span>` (unknown classes are dropped, so the
    hover would be invisible anyway), the scrollable table `<figure>` (same), and — the one that
    matters — a fenced code block, which has to be a code macro or Confluence renders it as a
    paragraph. Mermaid is left as its source in that macro: there is no server-side mermaid in
    Confluence, and a diagram nobody can read beats source nobody can copy.
    """
    body = markdown_html(content)
    body = _TABLE_FIGURE.sub(r"\1", body)
    body = _GLOSSARY_SPAN.sub(lambda m: m.group(2), body)
    return _CODE_BLOCK.sub(lambda m: _code_macro(m.group(1) or "", m.group(2)), body)


def _confluence_name(doc: Doc) -> str:
    return f"{doc.anchor}.xhtml"


def confluence_index(docs: list[Doc]) -> str:
    """A page listing what to import, in the order to import it.

    Plain links rather than `<ac:link>`: the pages don't exist until someone imports them, and a
    storage-format link to a missing page is an error, while a list of filenames is readable
    whatever the importer does with it.
    """
    items = []
    for domain, group in _grouped(docs):
        rows = "".join(
            f"<li><code>{html_mod.escape(_confluence_name(doc))}</code> — "
            f"{html_mod.escape(doc.title)}</li>"
            for doc in group
        )
        items.append(f"<li><strong>{html_mod.escape(domain)}</strong><ul>{rows}</ul></li>")
    return (
        "<p>Generated by specky. One file per document, in Confluence storage format — "
        "paste each one into a page's source editor, or feed the folder to a storage-format "
        "importer.</p>"
        f"<ul>{''.join(items)}</ul>\n"
    )


# --- PDF ---------------------------------------------------------------------------------


def _write_pdf(source: Path, target: Path) -> str | None:
    """Run `weasyprint` if it's on PATH. Returns an error line, or None on success.

    Never a hard dependency and never installed on the user's behalf: a docs plugin that pulls in a
    rendering engine to make one optional file is a bad trade, and every browser already prints the
    single page correctly.
    """
    binary = shutil.which("weasyprint")
    if not binary:
        return (
            "weasyprint not found, so no PDF was written — open the single page above and "
            "print it, or install the engine (`uv tool install weasyprint`, or `pipx install "
            "weasyprint`) and re-run with --pdf"
        )
    proc = subprocess.run(
        [binary, str(source), str(target)], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0 or not target.exists():
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return f"weasyprint failed ({proc.returncode}): {detail[-1] if detail else 'no output'}"
    return None


# --- the command -------------------------------------------------------------------------


def run_export(
    repo_root: Path,
    mode: str = "single-page",
    include_history: bool = False,
    pdf: bool = False,
    title: str = "Documentation",
) -> Export:
    if mode not in MODES:
        raise ValueError(f"unknown export mode {mode!r} (expected one of {', '.join(MODES)})")
    docs, history_skipped = collect(repo_root, include_history=include_history)
    out_dir = repo_root / EXPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    result = Export(mode=mode, doc_count=len(docs), history_skipped=history_skipped)

    if mode == "confluence":
        # Rewritten in place rather than emptied: the export dir is where a single page and a PDF
        # from an earlier run also live, and deleting someone's PDF because they then ran
        # --confluence would be a surprise.
        for doc in docs:
            target = out_dir / _confluence_name(doc)
            target.write_text(confluence_body(doc.content))
            result.written.append(target)
        index = out_dir / CONFLUENCE_INDEX
        index.write_text(confluence_index(docs))
        result.written.append(index)
        result.notes.append(
            "mermaid diagrams are exported as their source in a code macro — Confluence storage "
            "format takes no inline SVG"
        )
    else:
        glossary = load_glossary(repo_root)
        page, any_source, any_rendered = single_page_html(docs, glossary, title=title)
        target = out_dir / SINGLE_PAGE_NAME
        target.write_text(page)
        result.written.append(target)
        if any_source and not any_rendered:
            result.notes.append(
                "docs contain ```mermaid``` diagrams but none could be rendered (fenced source "
                "left as-is). Run `specky setup-diagrams` once, then re-export"
            )
        if pdf:
            error = _write_pdf(target, out_dir / PDF_NAME)
            if error:
                result.notes.append(error)
            else:
                result.pdf = out_dir / PDF_NAME
                result.written.append(result.pdf)

    if result.bytes_written > LARGE_EXPORT_BYTES:
        result.notes.append(
            f"that's {result.bytes_written // 1_000_000} MB across {len(result.written)} file(s) — "
            "large for an attachment; the HTML viewer (`specky render-html`) is the better link"
        )
    if history_skipped:
        result.notes.append(
            f"{history_skipped} per-commit doc(s) under `specs/history/` were left out — pass "
            "--include-history to include them"
        )
    return result


def report_lines(result: Export) -> list[str]:
    lines = [f"wrote {path.name}" for path in result.written[:5]]
    if len(result.written) > 5:
        lines.append(f"…and {len(result.written) - 5} more file(s)")
    lines.append(f"{result.doc_count} doc(s) exported to {EXPORT_DIR}/ ({result.mode})")
    lines += result.notes
    return lines
