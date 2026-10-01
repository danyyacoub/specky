---
commits: [86954aae0ea324b9a3dbf81d74c39d6bd1b13b45, 091fadbcb35487ceac59bba0843b2451adf98b3a, 3372b4f4594b69f58e692eb2d020c27ba6a4cfe5, b09bcde09a6396dd92ff2898fb0a5f1100520c62, 3437ecd376e081a909520e776f496cec8fa533d1, e5eb576b67661c2959cf599f1774fd244cc8f0aa, a55aa8c006a4aa35894e9984c3111747c3db9694, 1baa8418e0922cecfde88000866dfac4ee259dbd, cae48b3d782069d39d6e6b771584788052b2e041, 7b1ab01c9d215f3f430bfd0be67db3dbdea3c2ef, dfe39639303069a9000b76f62789ddfcb9149683, 5ee61bd440ef55ff0830251b50e3c2f1aeb90859, 47df0914e4c8ed32eb0f7515b054a6cd503d12e8, 0d626026ca0e7e01889364afd01153ec422c0ab6]
branch: main
impact: fix
features: [specs/rendering/matrix-tables.md, specs/rendering/html-viewer-shell.md, specs/rendering/home-activity-brief.md, specs/documentation/auto-commit-docs.md, specs/documentation/business-logic-filtering.md, specs/chat/serve-access-control.md, specs/rendering/workflow-stepper.md, specs/documentation/feature-sync.md]
---

# Generated feature docs keep their real path and reject agent narration

- **Date:** 2026-09-30T11:27:25+02:00 → 2026-10-01T10:24:23+02:00
- **Author:** dany <dany.yacoub@gmail.com>
- **Commits:**
    - `86954aae` feat(rendering): add ```matrix scenario tables that run as tests
    - `091fadbc` feat(viewer): sort workflows before features in each sidebar domain
    - `3372b4f4` feat(rendering): brief recent activity from the docs when git history is synthetic
    - `b09bcde0` chore: release 0.2.0
    - `3437ecd3` feat(history): never document skipped, bot, or ignored commits
    - `e5eb576b` chore: release 0.2.1
    - `a55aa8c0` feat(history): document only commits that change business logic
    - `1baa8418` feat(rendering): show what changed, by feature, on the home page
    - `cae48b3d` chore: release 0.2.2
    - `7b1ab01c` fix(chat): name the unreachable provider and log server-side failures
    - `dfe39639` chore: release 0.2.3
    - `5ee61bd4` fix(rendering): render workflow steps after a lead-in or under subheadings
    - `47df0914` fix(generator): keep agent narration out of generated docs
    - `0d626026` chore: release 0.2.4

## What changed

When specky writes or updates a feature doc, the model is told the doc's real path and to answer only in its reply, and a reply that isn't a doc (no `#` title and no `##` section) is refused and parked in `.specky/pending/` instead of saved. Before, a reply of pure commentary could be written over an existing doc as its entire body.

## Why

An agent CLI provider read a hardcoded `specs/…` path as a task in a repo whose docs live under `_specs/`, wrote the doc itself and returned only narration, which specky then saved over three docs.
