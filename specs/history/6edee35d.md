# Commit 6edee35d

- **Date:** 2026-09-11T14:46:17+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Message:** Generate per-feature/workflow reference docs, not just commit logs

Feature docs now auto-sync with code changes. Generator.py classifies each commit: if it affects documented features (skips refactors/formatting/config-only), it generates or updates `specs/<domain>/<topic>.md` in place—revising existing content rather than appending. `update_modules_index()` keeps `specs/MODULES.md` current. Wired into commit hook and `specky sync`. Specs/history/ becomes supplementary changelog trail; feature docs are the reference.
