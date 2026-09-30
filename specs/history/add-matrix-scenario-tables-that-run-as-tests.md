---
commits: [86954aae0ea324b9a3dbf81d74c39d6bd1b13b45, 091fadbcb35487ceac59bba0843b2451adf98b3a, 3372b4f4594b69f58e692eb2d020c27ba6a4cfe5, b09bcde09a6396dd92ff2898fb0a5f1100520c62, 3437ecd376e081a909520e776f496cec8fa533d1, e5eb576b67661c2959cf599f1774fd244cc8f0aa, a55aa8c006a4aa35894e9984c3111747c3db9694, 1baa8418e0922cecfde88000866dfac4ee259dbd, cae48b3d782069d39d6e6b771584788052b2e041, 7b1ab01c9d215f3f430bfd0be67db3dbdea3c2ef]
branch: main
impact: fix
features: [specs/rendering/matrix-tables.md, specs/rendering/html-viewer-shell.md, specs/rendering/home-activity-brief.md, specs/documentation/auto-commit-docs.md, specs/documentation/business-logic-filtering.md, specs/chat/serve-access-control.md]
---

# Spec Assistant errors now name the failing provider URL and cause, and server failures log a traceback

- **Date:** 2026-09-30T11:27:25+02:00 → 2026-09-30T18:53:34+02:00
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

## What changed

When the Spec Assistant can't reach its provider, the error now names the URL it was calling and the underlying cause instead of the SDK's bare "Connection error."; on Bedrock, an unresolvable host also names the region and points at `aws_region` / `SPECKY_AI_AWS_REGION`, since a region can run Bedrock without a Mantle endpoint. `specky serve` now returns 400 only for bad requests (no question, a non-JSON body, a step that can't be taken) and returns 500 with the error for anything that failed server-side, writing `specky serve: POST /chat failed` and the traceback to stderr so a container's logs show it.

## Why

A deployed Spec Assistant on Bedrock in eu-west-3 answered every question with a 400 reading only "Connection error." — Mantle has no host in that region, and nothing in the server's logs said so; the error now says why and the server's own failures are visible in its logs.
