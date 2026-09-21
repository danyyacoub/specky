---
sha: 7f5dbee472d6d1d4ef98b5d0c620626593861210
---

# Commit 7f5dbee4

- **Date:** 2026-09-21T22:03:00+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Message:** feat(chat): modernize the Spec Assistant from launcher to thinking overlay

This commit modernizes the Spec Assistant UI and its supporting behavior. The launcher becomes a blue-to-violet gradient pill with a ⌘/Ctrl + . shortcut (the gradient is reserved for the assistant's identity, while buttons and links keep the plain accent), the panel slides in on open rather than on restore, closes on Esc, and an empty conversation now shows page-aware suggested prompts that fill the input without sending.

The busy indicator is reworked into a floating pill over the bottom of the log—featuring a spinning gradient ring, shimmering status text, an elapsed-seconds clock, and a Stop button—and Stop aborts the in-flight fetch. A related fix makes `speckyFetch` rethrow `AbortError` instead of misreading it as "this origin isn't the API" and switching the base URL.

The composer becomes an auto-growing textarea (Enter sends, Shift+Enter adds a line) with a segmented intent switch and round send button; sources render as pills, Copy becomes an icon button, and error/note text renders `code` as code. Finally, the @ picker gains arrow-key navigation and builds its items from text nodes rather than `innerHTML`.
