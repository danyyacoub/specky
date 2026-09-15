---
sha: c13cfb7fc715d515032c82116e0c9d59b21e158b
---

# Commit c13cfb7f

- **Date:** 2026-09-15T15:34:30+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Message:** feat: ship a marketplace manifest so a checkout is installable as a plugin

Added marketplace manifest so `claude plugin marketplace add /path/to/specky` works. Validation didn't catch the missing file — `claude plugin validate` passes a bare `plugin.json`, but marketplace commands require the manifest. Now the documented install path actually works.
