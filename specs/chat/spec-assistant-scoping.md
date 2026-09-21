---
type: feature
tags: [chat, search, documentation]
---

# Chat — Spec Assistant Scoping

## What It Does

When asking the Spec Assistant a question or describing a change to draft, you can type `@` to narrow your search to a specific module (documentation domain) or feature/workflow. An explore question searches within that scope first, falling back to full search if nothing matches; a draft request places its change there — `@feature:` names the exact doc it updates, so the draft skips asking where it belongs. Answers can also include interactive HTML content rendered safely in an embedded preview.

## How It Works

1. **Type `@` in the chat input** — a dropdown appears listing available modules and features, the first one highlighted. The picker only opens on an `@` that starts a word, so typing an email address doesn't hijack Enter.
2. **Select a module or feature** — the arrow keys move the highlight (wrapping at either end), and Enter or Tab takes the highlighted item; a click works too. The widget inserts a scope token (`@module:<name>` or `@feature:<slug>`) into your question. Esc closes the picker and leaves the panel open.
3. **Submit your question** — the server strips the token and uses it to filter search results to that scope.
4. **Search fallback** — if the scope returns no results, the server searches all docs without scoping.
5. **Receive an answer with optional HTML** — the AI provider may include one fenced `html block in the response, which renders in a sandboxed preview below the prose answer.

## Outcomes

| Input | Result |
|-------|--------|
| Question with no scope | Full search across all docs; prose answer only |
| Question with `@module:<name>` | Search limited to that domain; falls back to full search if empty |
| Question with `@feature:<slug>` | Search limited to that feature/workflow; falls back if empty |
| Response with ```html snippet | Prose answer + interactive HTML preview (no scripts, CSP-protected) |
| Response with prose only | Prose answer displayed as before |

## Acceptance Tests

| Given | When | Then |
|-------|------|------|
| Chat widget open | Type `@` | Dropdown appears with available modules and features |
| Chat widget open | Type an email address containing `@` | No dropdown appears; Enter submits the question normally |
| Dropdown showing | Select a module | Token `@module:<name>` inserted into input; question ready to send |
| Dropdown showing | Select a feature | Token `@feature:<slug>` inserted into input; question ready to send |
| Dropdown showing | Press ↓ twice, then Enter | The third item's token is inserted; nothing is sent |
| Dropdown showing | Press Esc | Dropdown closes; the panel stays open |
| Question with scope token submitted | Server processes question | Scope token removed before prompting AI; search filters by scope |
| Scoped search matches docs | Server retrieves context | Results limited to matching domain or feature |
| Scoped search finds nothing | Server retrieves context | Unscooped full search executes; no error shown |
| AI response includes ```html block | Widget receives answer | HTML renders in sandbox; prose appears above |
| AI response is prose only | Widget receives answer | Answer displays as before |
