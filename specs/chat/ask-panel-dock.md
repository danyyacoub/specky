---
type: feature
tags: [chat, rendering]
---

# Chat — Ask Panel Dock

## What It Does

The Ask panel is now a persistent sidebar docked to the edge of the screen instead of a floating popup, giving answers more room to breathe. It renders answers with the same formatting as the documentation—tables scroll smoothly, diagrams appear as images, and glossary terms highlight on hover. The panel adapts: on wide screens it pushes the content pane aside; on narrow screens it overlays the content and can be resized by dragging its edge.

## How It Works

1. **User opens the panel** — The Ask companion is accessible as a docked sidebar on the screen's right edge, taking 320–720px of width depending on user preference.

2. **Question is classified by intent** — The system recognizes whether the user is asking for exploration (discovery), a spec draft (documentation), or general Q&A, and adjusts how it answers accordingly.

3. **Answer is generated and rendered** — The model writes markdown. Before it reaches the reader, every HTML tag and attribute is checked against an allowlist; anything not permitted is escaped to text. This step happens *before* diagram rendering.

4. **Rendered answer is displayed** — Markdown becomes HTML with the same styling as the docs: tables gain scroll bars and zebra stripes, glossary terms get hover tooltips, and mermaid fences become static SVG diagrams (capped at 2 per answer).

5. **User can interact with the answer** — Spec drafts show a Copy button to grab the markdown. Links within the answer navigate to source docs. The panel width is remembered for the session if the user resizes it by dragging.

6. **Panel adapts to screen size** — Below 1100px width, the panel overlays the content instead of pushing it aside, preserving reading space on smaller screens.

## Outcomes

| Scenario | Panel Behavior | Answer Format |
|---|---|---|
| Wide screen (≥1100px) | Docked sidebar, content reflows left | Full rendering: formatted tables, diagrams, hovers |
| Narrow screen (<1100px) | Overlays content area | Full rendering: formatted tables, diagrams, hovers |
| Spec draft intent | Shows Copy button, no write-to-docs | Rendered markdown in panel only |
| Multiple questions | Appends to chat history | Each answer styled consistently |
| Heavy diagrams in answer | Caps at 2 mermaid fences | Fences beyond limit stay as text |

## Acceptance Tests

| Given | When | Then |
|---|---|---|
| Panel is closed | User clicks Ask icon | Panel opens docked to right edge at 320px wide |
| Panel is open at 320px | User drags the left edge handle | Panel width increases; content pane reflows or disappears based on screen size |
| Screen is 900px wide | Panel is open | Panel overlays content area instead of pushing it aside |
| User asks "draft a spec for retry logic" | Panel classifies intent and generates answer | Answer renders with formatted lists, code blocks, and tables; no write-to-docs endpoint exists |
| Answer contains 3 mermaid fences | System renders the answer | First 2 fences become static SVG; 3rd stays as markdown text |
| Model writes `<script>` tag in answer | Sanitizer processes the response | `<script>` tag and its contents are removed; text after it remains |
| User resizes panel to 550px | User closes browser and returns | Panel reopens at 550px (width remembered in session storage) |
