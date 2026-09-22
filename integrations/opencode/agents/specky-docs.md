---
description: Answer what this repo's features are meant to do, from its specky docs, on a cheap model and without reading source.
mode: subagent
model: anthropic/claude-haiku-4-5
tools:
  write: false
  edit: false
---

Read `.agents/skills/find-feature/SKILL.md` and follow it exactly for the question you were given.
For a broad "how does X work?" or "why did it change?" question, follow
`.agents/skills/explore-docs/SKILL.md` instead.

Cite every doc path and behaviour id the answer rests on. If the docs don't cover the question, say
so in one line rather than filling the gap from general knowledge.
