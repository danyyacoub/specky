---
sha: a8ab1b6a14db511eb49d3cf610d755f2958d401e
---

# Commit a8ab1b6a

- **Date:** 2026-09-13T01:33:17+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Message:** Correct the generated pr-comment doc

Doc was out of sync with code. Fixed four incorrect claims: empty ranges now print one line (not nothing), history docs are counted not listed, added docs quote their summaries, and `gh pr comment` is user's command not specky's. Added missing test scenarios (rename, unchanged-summary, merge-base) to match current behavior.
