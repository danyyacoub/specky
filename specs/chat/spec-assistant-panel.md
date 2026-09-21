---
type: feature
tags: [chat, rendering]
---

# Chat — Spec Assistant Panel

## What It Does

The Spec Assistant is the viewer's side panel for working with the docs: ask what they say, or draft a change to them. It is docked to the right edge of the screen rather than floating over the page, so an answer has room for tables and diagrams, and the reader can keep browsing while it is open. Answers use the same formatting as the docs — scrolling tables, diagrams drawn as images, glossary terms that explain themselves on hover. An explored question gets a short answer first, with the details one click away. A draft request walks through its steps in the same panel ([spec-drafting-workflow.md](spec-drafting-workflow.md)).

## How It Works

1. **Reader opens the panel** — the **Spec Assistant** button opens a docked sidebar on the right edge, 320–720px wide, which the reader can resize by dragging its edge.

2. **The question is routed by intent** — the server recognises whether the reader is exploring the docs or asking for a spec to be drafted, and the **Auto / Explore / Draft spec** chips can pin that choice when it reads a question the other way round.

3. **An explore answer leads with the short version** — the model writes a short answer that stands on its own (the fact, name or yes/no, and the doc it comes from), then the details. The panel shows the short answer, with a **Read more** button that expands the details and **Show less** that folds them away again.

4. **A draft request becomes a sequence of steps** — each step shows as a card with a progress row (Scope · Impact · Tests · Draft), the doc it targets, and the buttons that move it on. What the reader types next answers the card's question or corrects it, while a "Replying to the draft" bar is showing.

5. **Answers are made safe before they are shown** — the model writes markdown, and every HTML tag and attribute it contains is checked against an allowlist first. Anything not permitted is escaped to text, before diagrams are drawn.

6. **Answers are rendered like the docs** — tables gain scroll bars and zebra stripes, glossary terms get hover tooltips, and mermaid fences become static diagrams, at most 2 per answer.

7. **The panel follows the reader** — width, open state and pinned intent are remembered for the tab. A draft's current step is rebuilt on the next page, so its buttons keep working after the reader follows a cited source.

8. **The panel adapts to screen size** — below 1100px wide it overlays the content instead of pushing it aside.

## Outcomes

| Scenario | Panel Behavior | Answer Format |
|---|---|---|
| Wide screen (≥1100px) | Docked sidebar, content reflows left | Full rendering: formatted tables, diagrams, hovers |
| Narrow screen (<1100px) | Overlays content area | Full rendering: formatted tables, diagrams, hovers |
| Explore question | Short answer, plus **Read more** when there are details | Short answer alone first; details expand in place |
| Explore answer that is only a table or heading | Shown whole | No **Read more** — there's nothing shorter to lead with |
| Spec draft intent | Step cards with their own buttons; Copy on the finished draft | Nothing is written to the docs tree |
| Multiple questions | Appends to chat history | Each answer styled consistently |
| Heavy diagrams in answer | Caps at 2 mermaid fences | Fences beyond limit stay as text |

## Acceptance Tests

| Given | When | Then |
|---|---|---|
| Panel is closed | Reader clicks **Spec Assistant** | Panel opens docked to the right edge |
| Panel is open at 320px | Reader drags the left edge handle | Panel width increases; content pane reflows or is overlaid depending on screen size |
| Screen is 900px wide | Panel is open | Panel overlays content area instead of pushing it aside |
| Reader asks "how do refunds work?" | The answer arrives | Only the short answer shows, with a **Read more** button |
| An explore answer with details is showing | Reader clicks **Read more** | The details expand below the short answer and the button reads **Show less** |
| The model's answer is a single sentence | The answer arrives | No **Read more** button is shown |
| Reader asks "draft a spec for retry logic" | The server classifies the intent | The draft workflow starts and its first step shows as a card; no doc is written |
| Answer contains 3 mermaid fences | System renders the answer | First 2 fences become static SVG; 3rd stays as markdown text |
| Model writes `<script>` tag in answer | Sanitizer processes the response | `<script>` tag and its contents are removed; text after it remains |
| Reader resizes panel to 550px | Reader navigates to another page in the same tab | Panel reopens at 550px (width remembered in session storage) |
| A draft is waiting on the Impact step | Reader navigates to another page | The Impact card is rebuilt there and its buttons still work |
