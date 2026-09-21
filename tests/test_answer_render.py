"""The trust boundary in front of a chat answer, and the pipeline behind it.

Every test here is about markup written by a model, so "keeps working" and "stays safe" are the
same property: the sanitizer has to pass through enough of python-markdown's output that an answer
reads like a doc page, and nothing else.
"""

from pathlib import Path

import pytest

from specky import answer_render, diagram_render
from specky.answer_render import render_answer, sanitize_fragment

# --- the sanitizer: what a model may put on the reader's screen ---------------------------


def test_a_script_is_dropped_with_its_contents():
    """Not escaped-and-shown: a script body rendered as prose is still the model's code in the
    page, and it's never what the reader asked for."""
    out = sanitize_fragment("<p>before</p><script>alert(1)</script><p>after</p>")
    assert out == "<p>before</p><p>after</p>"


@pytest.mark.parametrize(
    "fragment",
    [
        "<iframe src=/x></iframe>",
        "<object data=x.swf></object>",
        "<embed src=x>",
        "<style>body{display:none}</style>",
        "<form action=/x><input name=q></form>",
        "<noscript>x</noscript>",
    ],
)
def test_the_tags_that_execute_or_fetch_are_dropped(fragment):
    assert "<" not in sanitize_fragment(fragment)


def test_an_event_handler_and_an_inline_style_are_dropped_but_the_tag_survives():
    out = sanitize_fragment('<p onclick="steal()" style="position:fixed" class="x">hi</p>')
    assert out == '<p class="x">hi</p>'


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "java\tscript:alert(1)",  # a browser ignores the tab; a naive check calls this relative
        "  javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox",
    ],
)
def test_a_link_to_a_scheme_that_runs_code_loses_its_href(href):
    out = sanitize_fragment(f'<a href="{href}">click</a>')
    assert "href" not in out
    assert out == "<a>click</a>"


@pytest.mark.parametrize(
    "href",
    ["specs/billing/refund-flow.html", "#outcomes", "https://example.com/x", "mailto:a@b.example"],
)
def test_the_links_an_answer_actually_needs_survive(href):
    out = sanitize_fragment(f'<a href="{href}">doc</a>')
    assert f'href="{href}"' in out


def test_an_external_link_gets_rel_noopener():
    assert 'rel="noopener noreferrer"' in sanitize_fragment('<a href="https://x.example">x</a>')


def test_an_image_is_dropped_because_an_img_src_is_a_network_call():
    out = sanitize_fragment('<p>see <img src="https://tracker.example/p.gif"> this</p>')
    assert "img" not in out
    assert out == "<p>see  this</p>"


def test_an_unknown_tag_keeps_its_text_so_a_stray_tag_is_not_a_hole_in_the_answer():
    assert sanitize_fragment("<section>the answer</section>") == "the answer"


def test_a_table_survives_whole():
    out = sanitize_fragment(
        "<table><thead><tr><th>Case</th></tr></thead>"
        "<tbody><tr><td>Full</td></tr></tbody></table>"
    )
    assert out.startswith("<table><thead><tr><th>Case</th>")
    assert out.endswith("</tbody></table>")


def test_a_code_block_keeps_its_language_class():
    """`language-mermaid` is the class the diagram step matches on, which is the whole reason
    `class` is allowed on every tag."""
    out = sanitize_fragment('<pre><code class="language-mermaid">graph TD\n  A --&gt; B</code></pre>')
    assert '<code class="language-mermaid">' in out
    assert "A --&gt; B" in out


def test_a_class_value_is_filtered_to_names():
    out = sanitize_fragment('<p class="ok\\"><script>x</script>">hi</p>')
    assert "script" not in out
    assert 'class="ok' in out


def test_an_unclosed_tag_is_closed_rather_than_leaking_into_the_panel():
    assert sanitize_fragment("<div><p>half an answer") == "<div><p>half an answer</p></div>"


def test_a_stray_close_tag_is_ignored():
    assert sanitize_fragment("</div><p>answer</p>") == "<p>answer</p>"


def test_text_is_escaped():
    assert sanitize_fragment("5 < 6 & 7 > 2") == "5 &lt; 6 &amp; 7 &gt; 2"


def test_a_comment_is_dropped():
    assert sanitize_fragment("<p>a</p><!-- <script>x</script> -->") == "<p>a</p>"


# --- render_answer: the same pipeline a doc page gets -------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo root with a docs tree — no git needed, nothing here reads history."""
    (tmp_path / "specs").mkdir()
    return tmp_path


def test_markdown_becomes_the_markup_a_doc_page_would_get(repo):
    body = render_answer(repo, "## Refunds\n\nA refund is **full** or partial.")
    assert "<h2>Refunds</h2>" in body
    assert "<strong>full</strong>" in body


def test_a_markdown_table_is_wrapped_in_the_scrollable_figure(repo):
    body = render_answer(repo, "| Case | Result |\n|---|---|\n| Full | Refunded |")
    assert '<figure class="tw"><table>' in body
    assert "<td>Refunded</td>" in body


def test_a_glossary_term_gets_the_hover_span_a_doc_page_would_get(repo, monkeypatch):
    """The definition itself is already in the page (`SPECKY_GLOSSARY`), keyed off `data-term` —
    so an answer only has to carry the same span a doc body does, tabindex included."""
    monkeypatch.setattr(
        answer_render, "_glossary", lambda _root: (("micro-doc", "One commit's summary."),)
    )
    body = render_answer(repo, "The micro-doc holds the summary.")
    assert '<span class="gl" data-term="micro-doc" tabindex="0">micro-doc</span>' in body


def test_a_mermaid_fence_becomes_a_rendered_figure(repo, monkeypatch):
    monkeypatch.setattr(diagram_render, "render_mermaid_svg", lambda src: f"<svg>{src.strip()}</svg>")
    body = render_answer(repo, "Flow:\n\n```mermaid\ngraph TD\n  A --> B\n```")
    assert '<figure class="flow"><svg>graph TD\n  A --> B</svg></figure>' in body


def test_a_fence_the_renderer_cannot_draw_degrades_to_its_own_source(repo, monkeypatch):
    """What a machine without `specky setup-diagrams` gets: the source text, not an error."""
    monkeypatch.setattr(diagram_render, "render_mermaid_svg", lambda _src: None)
    body = render_answer(repo, "```mermaid\ngraph TD\n  A --> B\n```")
    assert "figure" not in body
    assert "graph TD" in body


def test_only_the_first_few_fences_are_drawn(repo, monkeypatch):
    """Each fence is a `node` subprocess, so the bound can't depend on the model's restraint."""
    calls = []

    def fake(source):
        calls.append(source)
        return "<svg>x</svg>"

    monkeypatch.setattr(diagram_render, "render_mermaid_svg", fake)
    fences = "\n\n".join(
        f"```mermaid\ngraph TD\n  A{n} --> B{n}\n```"
        for n in range(answer_render.MAX_ANSWER_DIAGRAMS + 2)
    )
    body = render_answer(repo, fences)
    assert len(calls) == answer_render.MAX_ANSWER_DIAGRAMS
    assert body.count('<figure class="flow">') == answer_render.MAX_ANSWER_DIAGRAMS
    assert "A3" in body  # the undrawn ones are still shown, as source


def test_the_svg_a_model_writes_is_dropped_while_the_renderers_survives(repo, monkeypatch):
    """The reason the sanitizer runs *before* the diagram step: it never has to understand SVG,
    because the only SVG in the output is the one this codebase produced."""
    monkeypatch.setattr(diagram_render, "render_mermaid_svg", lambda _src: "<svg>drawn</svg>")
    body = render_answer(
        repo,
        '<svg onload="alert(1)"><path d="M0 0"/></svg>\n\n```mermaid\ngraph TD\n  A --> B\n```',
    )
    assert "onload" not in body
    assert "M0 0" not in body
    assert body.count("<svg>") == 1
    assert "<svg>drawn</svg>" in body
