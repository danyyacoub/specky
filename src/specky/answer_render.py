"""A chat answer's markdown turned into the HTML the Ask panel shows.

An answer is the one thing in the viewer written by a model rather than by this codebase, and it
lands in the reader's page as markup. So it goes through the same pipeline a doc page does —
markdown, glossary hover terms, scrollable tables, ```mermaid``` fences as static SVG (see
html_render and diagram_render) — with one step the doc path doesn't need: everything the model wrote is scrubbed
against an allowlist first.

Order matters, and it's the reason the sanitizer can be small. The model's markup is sanitized
*before* the diagram step, so the only `<svg>` in the output is the one our own renderer produced
(itself already run through `diagram_render._scrub_svg`). The sanitizer therefore never has to
understand SVG, and an `<svg>` the model wrote itself is escaped to text like any other tag it
isn't allowed to use.

Everything here is a fragment, never a document: no `<html>`, `<head>` or `<body>`, because the
panel inserts it into a page that already has those.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path

from specky import diagram_render, html_render

# Each fence is a `node` subprocess (diagram_render.render_mermaid_svg), so one answer must not be
# able to fan out into a dozen of them. The prompt asks for at most one; this is the bound that
# doesn't depend on the model honouring it. Fences past it are left as their own source text.
MAX_ANSWER_DIAGRAMS = 2

# What a model may put in the panel. Deliberately the vocabulary python-markdown emits, plus the
# few inline tags a model reaches for by hand — not "HTML minus the dangerous bits", which is the
# allowlist that keeps growing until something gets through.
_ALLOWED_TAGS = frozenset(
    """a b blockquote br code del div em h1 h2 h3 h4 h5 h6 hr i li ol p pre s span strong sub sup
    table tbody td th thead tr ul""".split()
)
_VOID_TAGS = frozenset({"br", "hr"})
# Dropped *with their contents*, rather than having the contents kept as text: a `<script>` body
# shown as prose is still the model's code on the reader's screen, and an `<svg>` the model wrote
# is a pile of path data. Anything else unknown (`<section>`, `<img>`, a typo'd tag) keeps its text,
# which is what makes a stray `<foo>` in an answer read as an answer rather than a hole in one.
_DROP_WITH_CONTENTS = frozenset(
    {"script", "style", "iframe", "object", "embed", "template", "noscript", "svg", "math", "form"}
)
# `class` survives on every tag for one specific reason: python-markdown marks a fenced block as
# `<code class="language-mermaid">`, and that class is what the diagram step matches on. Values are
# filtered to word characters anyway.
_GLOBAL_ATTRS = frozenset({"class"})
_TAG_ATTRS = {"a": frozenset({"href", "title"}), "th": frozenset({"align"}), "td": frozenset({"align"})}
_CLASS_VALUE = re.compile(r"[^\w -]")

_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")
_LINK_SCHEMES = frozenset({"http", "https", "mailto"})
# Stripped before the scheme check: a browser ignores control characters and whitespace inside a
# URL, so `java\tscript:alert(1)` is a working javascript: link that a naive check calls relative.
_URL_NOISE = re.compile(r"[\s\x00-\x1f\x7f]")


def _safe_href(value: str) -> str | None:
    """A link a reader can be given, or None. Relative paths and fragments pass (the panel links
    sources to their own doc pages); an explicit scheme has to be one of `_LINK_SCHEMES`."""
    cleaned = _URL_NOISE.sub("", value)
    if not cleaned:
        return None
    match = _SCHEME.match(cleaned)
    if match and match.group(1).lower() not in _LINK_SCHEMES:
        return None
    return cleaned


class _Sanitizer(HTMLParser):
    """Rebuilds a fragment from allowed tags and attributes, escaping everything else.

    Rebuilding rather than removing is the point: the output contains only tags this class chose
    to write, so there's no original markup left for a clever encoding to survive in.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._open: list[str] = []
        self._suppress = 0

    def result(self) -> str:
        # A model's fragment can be unbalanced (a `<div>` it never closed). Close what's open in
        # reverse order rather than emitting markup that leaks into the rest of the panel.
        return "".join(self._out) + "".join(f"</{tag}>" for tag in reversed(self._open))

    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        allowed = _GLOBAL_ATTRS | _TAG_ATTRS.get(tag, frozenset())
        parts = []
        for name, value in attrs:
            name = name.lower()
            if name not in allowed or value is None:
                continue  # every `on*` and `style` attribute lands here
            if name == "class":
                value = _CLASS_VALUE.sub("", value).strip()
                if not value:
                    continue
            elif name == "href":
                href = _safe_href(value)
                if href is None:
                    continue
                value = href
            parts.append(f' {name}="{html.escape(value, quote=True)}"')
        if tag == "a" and any(p.startswith(' href="') for p in parts):
            parts.append(' rel="noopener noreferrer"')
        return "".join(parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _DROP_WITH_CONTENTS:
            self._suppress += 1
            return
        if self._suppress or tag not in _ALLOWED_TAGS:
            return
        self._out.append(f"<{tag}{self._attrs(tag, attrs)}>")
        if tag not in _VOID_TAGS:
            self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._suppress or tag in _DROP_WITH_CONTENTS or tag not in _ALLOWED_TAGS:
            return
        self._out.append(f"<{tag}{self._attrs(tag, attrs)}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _DROP_WITH_CONTENTS:
            self._suppress = max(self._suppress - 1, 0)
            return
        if self._suppress or tag in _VOID_TAGS or tag not in self._open:
            return
        # Unwind to the matching tag: a `</p>` closing over an unclosed `<em>` closes the em too,
        # which is what a browser does and what keeps `self._open` honest.
        while self._open:
            open_tag = self._open.pop()
            self._out.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self._suppress:
            self._out.append(html.escape(data))

    def handle_comment(self, data: str) -> None:
        pass

    def handle_decl(self, decl: str) -> None:
        pass

    def unknown_decl(self, data: str) -> None:
        pass

    def handle_pi(self, data: str) -> None:
        pass


def sanitize_fragment(fragment: str) -> str:
    """Model-written markup reduced to the allowlist above. The trust boundary for chat answers."""
    parser = _Sanitizer()
    parser.feed(fragment)
    parser.close()
    return parser.result()


@lru_cache(maxsize=4)
def _glossary(repo_root: str) -> tuple[tuple[str, str], ...]:
    """`specs/GLOSSARY.md`'s terms, read once per repo per process.

    `specky serve` is long-lived and every question would otherwise re-read the file. The cost is
    that a GLOSSARY.md edit reaches answers only after a server restart — the same restart the
    rest of a docs change needs anyway (`specky index`, `specky render-html`).
    """
    return tuple(html_render.load_glossary(Path(repo_root)).items())


def render_answer(repo_root: Path, markdown_text: str) -> str:
    """One answer's markdown as panel HTML: sanitized, glossary-linked, tables wrapped, diagrams
    drawn.

    Same order as `html_render.render_doc_body` so an answer reads like a doc page, with the
    sanitizer inserted where the model's markup stops being the model's — before any of our own
    markup (hover spans, `figure.tw`, diagram SVG) is added, so none of it is ever scrubbed.
    """
    fragment = sanitize_fragment(html_render.markdown_html(markdown_text))
    fragment = html_render.link_glossary(fragment, dict(_glossary(str(repo_root))))
    body, _source, _rendered = diagram_render.render_mermaid_blocks(
        html_render._wrap_tables(fragment), limit=MAX_ANSWER_DIAGRAMS
    )
    return body
