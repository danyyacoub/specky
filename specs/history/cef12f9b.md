---
sha: cef12f9b0c0ebd27e9419fc16cb956c325fffb84
---

# Commit cef12f9b

- **Date:** 2026-09-21T13:42:29+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Message:** refactor(viewer): tidy the type icons and diagram sizing rules

Refactored the viewer's type-icon handling and diagram CSS to remove duplication. The search results' icon map was a hand-maintained JS copy of the Python `_TYPE_ICONS` dict, so `SEARCH_JS` is now generated from the Python map (like `DIAGRAM_JS` gets its tokens), preventing drift. Diagram sizing rules (`figure.flow svg`, `.fx` overflow) were duplicated per scope; they're now shared globally, leaving only margins and the chat panel's width bound per scope. Also renamed the title-comparison helper to `_title_key` with a clarifying docstring, rewrapped a module docstring, bumped icons/fallback constants to module top-level, and added a note explaining what `DIAGRAM_JS` borrows from `app.js`.
