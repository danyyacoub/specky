---
sha: 6d8bed3dfedc312da6e7418607ffad1f2a60d806
---

# Commit 6d8bed3d

- **Date:** 2026-09-16T11:03:09+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Message:** docs: drop CLAUDE.md command table and architecture section

Dropped outdated command table and architecture section from CLAUDE.md. Both duplicated `--help` output and specs/MODULES.md docs, which drift as code changes. Replaced with pointers to those canonical sources plus list of non-obvious CLI gotchas. Keeps docs naming convention — only rule that doesn't live elsewhere.
