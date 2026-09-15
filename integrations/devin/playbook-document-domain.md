<!--
The body of a Devin playbook (Settings -> Resources -> Playbooks). Playbooks live in Devin's UI, not
in the repo, so this is deliberately thin: it points at the vendored copy of `SKILL.md` rather than
duplicating it, and upgrading specky is then a `cp` instead of a re-paste. See ./README.md.

Everything below the marker is the playbook body.
-->

# Document a domain

Generate or update the functional documentation for one domain/module of this codebase, keeping
`specs/MODULES.md` and `specs/GLOSSARY.md` in sync.

## Procedure

The full procedure — file naming, the doc template, the frontmatter and acceptance-test rules, when
a diagram earns its place, how to update an existing doc — is checked into this repo at
`.devin/document-domain.md`. **Read that file first and follow it step by step.** It is the same
procedure every other agent working on this repo uses, so a doc written from memory instead will
come out shaped differently from its neighbours.

If `.devin/document-domain.md` doesn't exist, stop and say so rather than improvising: it means this
repo hasn't finished setting specky up, and the fix is a one-line `cp` from a specky checkout.

## Inputs

Ask for these if the prompt didn't give them:

1. **Domain** — which module or area to document (e.g. `billing`, `auth`, `search`).
2. **Files** (optional) — where to focus. Otherwise discover them by searching for the domain name.

## Before you start

- `specky search "<domain>"` — find the docs that already exist. Updating one is almost always
  right; a second doc for a topic that already has one is almost always wrong.
- `specky tags` — reuse an existing tag rather than inventing a synonym for it.
- Read `specs/PRODUCT.md` and `specs/GLOSSARY.md`. The doc has to use the vocabulary the rest of
  `specs/` already uses.

## When you're done

- `specky index` then `specky search` for a term from the new doc, to prove it's in the index.
- `specky check --advisory` — reports without failing, so you can see whether the change left
  anything else stale.
- Report which files you created or updated, whether you added a `MODULES.md` section or a glossary
  term, and anything that needs a human's judgement (business logic you had to guess at is the
  common one — say so rather than writing it confidently).

Don't touch `specs/history/`. Those are generated, one per commit.
