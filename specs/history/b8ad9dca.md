---
sha: b8ad9dcac5c4d2624664c7c472c139b5f243509b
impact: internal
features: [specs/integration/skill-model-selection.md]
---

# specky pitches itself to AI-written codebases and cheaper doc models

- **Date:** 2026-09-22T18:02:57+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Message:** docs: pitch the readme at AI-written code and lower-cost doc models

## What changed

The README now opens by framing specky around AI-written code and names its three audiences: AI agents, developers and product managers. It adds a section explaining that you can set `[skills] model = "haiku"` in `specky.toml` to route doc lookups and writing to a lower-cost model in Claude Code, and use `[ai] <task>_model` for the CLI and git hooks. No product behaviour changed.

## Why

The commit message says the goal is to pitch the README at AI-written code and lower-cost doc models.
