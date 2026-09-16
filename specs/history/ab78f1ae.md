---
sha: ab78f1ae7181d9c71d03c9dba1365c58c7306695
---

# Commit ab78f1ae

- **Date:** 2026-09-16T10:50:49+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Message:** docs: compact CLAUDE.md, defer detail to specs/

CLAUDE.md was trimmed from duplicated command documentation and architecture details down to a command reference table with links to specs/, plus a short list of key invariants. Reduces noise in the file by deferring flag documentation, full behavior descriptions, and architectural prose to where they're maintained in specs/ — single source of truth for detailed docs.
