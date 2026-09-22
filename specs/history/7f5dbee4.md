---
sha: 7f5dbee472d6d1d4ef98b5d0c620626593861210
impact: feature
features: [specs/chat/spec-assistant-panel.md]
---

# Spec Assistant gets a shortcut, suggested prompts and a stoppable thinking pill

- **Date:** 2026-09-21T22:03:00+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Message:** feat(chat): modernize the Spec Assistant from launcher to thinking overlay

## What changed

The Spec Assistant now opens with ⌘ . / Ctrl . (and the same keys or Esc close it), slides in when opened rather than on restore, and greets an empty conversation with page-aware suggested prompts that fill the input without sending. While a question is pending, a floating status pill with a spinner, elapsed-seconds clock and Stop button replaces the old status line; Stop gives up on the wait in the browser only, leaving the server's answer in the conversation. The composer is now an auto-growing textarea where Enter sends and Shift+Enter adds a line, the intent chips are a segmented switch, and the @ picker supports arrow-key navigation and Esc to close.

## Why

The panel was modernized from a plain launcher to a thinking overlay: waiting is now visible where the reader is looking, an empty conversation offers a way in, and stopping a request no longer breaks fetching for the origin.
