# vendor/mermaid-render

Render-time-only Node tool: turns one mermaid source string into an SVG via
[`beautiful-mermaid`](https://www.npmjs.com/package/beautiful-mermaid) (MIT, zero DOM
dependencies — runs in plain Node, no jsdom/browser needed). Called by
[`diagram_render.py`](../../diagram_render.py)'s `render_mermaid_svg()` via subprocess, once per
```mermaid``` fence, at `specky render-html` time.

Not shipped to readers of the generated site — the output is a plain `<svg>` embedded
in the page, no client-side JS. `node_modules/` here is gitignored, same as any other
Node project's — which is also why it isn't in the built wheel, and why *this* directory
is only one of the places the tool may live. [`mermaid_tool.py`](../../mermaid_tool.py)
owns that resolution: `$SPECKY_MERMAID_DIR`, then `~/.cache/specky/mermaid-render`, then
this copy.

One-time setup (needs Node — this is the only place in specky that does):

```bash
specky setup-diagrams                             # any install; populates ~/.cache/specky
npm install --prefix src/specky/vendor/mermaid-render   # or, in a checkout, straight into here
```

If neither has been run, `render_mermaid_svg()` returns `None` and `diagram_render.py`
leaves the fenced source as plain text instead of failing the render — same behavior as
a doc with no diagrams. `specky render-html` prints a one-time hint if it detects this.

Protocol (`render.mjs`): reads one JSON object from stdin, `{source, options}` — `options`
is `beautiful-mermaid`'s own theming shape (`fg`/`line`/`accent`/`muted`/`surface`/
`border`/`font`/...). Writes the SVG to stdout on success; on failure, writes a message
to stderr and exits non-zero.
