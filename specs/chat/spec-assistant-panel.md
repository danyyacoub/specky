---
type: feature
tags: [chat, rendering]
---

# Chat — Spec Assistant Panel

## What It Does

The Spec Assistant is the viewer's side panel for working with the docs: ask what they say, or draft a change to them. It is docked to the right edge of the screen rather than floating over the page, so an answer has room for tables and diagrams, and the reader can keep browsing while it is open. Answers use the same formatting as the docs — scrolling tables, diagrams drawn as images, glossary terms that explain themselves on hover. An explored question gets a short answer first, with the details one click away. A draft request walks through its steps in the same panel ([spec-drafting-workflow.md](spec-drafting-workflow.md)).

## How It Works

1. **Reader opens the panel** — the **Spec Assistant** button (bottom right, showing its shortcut) or **⌘ .** / **Ctrl .** from anywhere on the page opens a docked sidebar on the right edge, 560px wide, which the reader can resize by dragging its edge. The same shortcut closes it, and so does Esc while focus is inside it. Its width is capped relative to the window so the doc column keeps at least 360px to be read in, even after the window shrinks or the nav rail comes back. Opening the panel also collapses the nav rail; a titlebar button brings it back, and closing the panel restores it.

2. **The question is routed by intent** — the server recognises whether the reader is exploring the docs or asking for a spec to be drafted, and the **Auto / Explore / Draft spec** switch in the composer bar, beside Send, can pin that choice when it reads a question the other way round. The input grows with what's typed: Enter sends, Shift+Enter starts a new line.

3. **An explore answer leads with the short version** — the model writes a short answer that stands on its own (the fact, name or yes/no, and the doc it comes from), then the details. The panel shows the short answer, with a **Read more** button that expands the details and **Show less** that folds them away again.

4. **A draft request becomes a sequence of steps** — each step shows as a card with a progress row (Scope · Impact · Tests · Draft), the doc it targets, and the buttons that move it on. What the reader types next answers the card's question or corrects it, while a "Replying to the draft" bar is showing.

5. **Answers are made safe before they are shown** — the model writes markdown, and every HTML tag and attribute it contains is checked against an allowlist first. Anything not permitted is escaped to text, before diagrams are drawn.

6. **Answers are rendered like the docs** — tables gain scroll bars and zebra stripes, glossary terms get hover tooltips, and mermaid fences become static diagrams, at most 2 per answer. A table's cells wrap between words, never inside one: a table too wide for the panel scrolls sideways instead. Outside tables, a long unbroken path or hash still breaks, so it can't push the panel wider than it is.

7. **The panel follows the reader** — width, open state and pinned intent are remembered for the tab. A draft's current step is rebuilt on the next page, so its buttons keep working after the reader follows a cited source.

8. **The panel adapts to screen size** — below 1100px wide it overlays the content instead of pushing it aside.

9. **Waiting is visible where the reader is looking** — while an answer is pending, a floating status pill shows right above the input (the header status span was easy to miss), naming what's happening ("Thinking…", or the draft step), counting the seconds, with a **Stop** button. Stop gives up on the wait in the browser only: a stopped draft step leaves the draft where it was, but the server still finishes a stopped question and keeps that answer in the conversation, so a follow-up can refer to it.

10. **An empty conversation offers a way in** — a new or reset conversation shows a short welcome and a few suggested prompts drawn from the page being read. Clicking one fills the input; it is never sent on its own.

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
| Panel is closed | Reader presses ⌘ . (Ctrl . off a Mac) | Panel opens; pressing it again closes it |
| Panel is open, focus in the input | Reader presses Esc | Panel closes and focus returns to the **Spec Assistant** button |
| Reader typed a line | Reader presses Shift+Enter | A new line starts in the input; nothing is sent |
| A question is pending | Reader clicks **Stop** | The status pill hides and a "Stopped." note appears in the conversation |
| Conversation is empty on a feature doc | Reader clicks a suggested prompt | The prompt fills the input and is not sent |
| An answer's table has short labels in one column, at the default panel width | The answer is shown | Every cell wraps between words ("Resolve range" over two lines, never "Resolv / e range") |
| An answer's table cell holds a token with no break point wider than the panel | The answer is shown | The table scrolls sideways inside its frame; the panel does not widen |
