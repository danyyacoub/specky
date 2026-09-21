---
sha: fb6c4c5d5abda0f2c39f42d9231eeeb1c6348ced
---

# Commit fb6c4c5d

- **Date:** 2026-09-21T12:19:33+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Message:** fix(generator): stop a section update stacking a doc on itself

This commit fixes a bug in the doc generator where a section update could stack a doc on top of itself. DeepSeek answered a one-sentence change to `## What It Does` by nesting the rest of the doc inside that one value, which `merge_sections` then spliced in as a single section, duplicating every heading. No existing guard caught it: nothing was lost, and lost-content measurement checked each repeated heading against its last, untouched copy.

To fix this, a section nested inside another's value is now lifted out and replaces its namesake in place, and a body repeating a `##` heading the old doc didn't have is refused and parked like any other bad rewrite. The spec files `specs/cli/document.md` and `specs/documentation/feature-sync.md` were updated to document the new "Repeated section" guard and the revised splice and measure steps.
