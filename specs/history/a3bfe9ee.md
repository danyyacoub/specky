---
sha: a3bfe9ee3b2f402bd8e128b52c218a265debff24
---

# Commit a3bfe9ee

- **Date:** 2026-09-13T00:02:00+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Message:** Add `specky check`, a CI gate for docs that go stale

Added `specky check`, CI gate that fails when commits change code described in docs without updating those docs. Derives coverage map from git history alone (zero AI cost, millisecond runtime), uses thresholds and recurrence patterns to filter false positives from bulk changes. Includes new CLI command, documentation, and CI workflow integration.
