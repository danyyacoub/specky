<!--
Append this to the repo's root AGENTS.md — the file Devin reads before it starts coding. It's a
snippet, not a file to copy wholesale: a repo's AGENTS.md is about the repo, and this is the
paragraph about its docs. See ./README.md.

Deliberately not named AGENTS.md: Devin looks for that filename in the project root "or anywhere
else", and a stray copy in a subdirectory is instructions nobody meant to give.
-->

## Documentation

Functional docs live in `specs/<domain>/<topic>.md`, committed alongside the code — kebab-case
topic, never `README.md`. `specs/MODULES.md` indexes them and `specs/GLOSSARY.md` holds the shared
vocabulary. They're maintained by [specky](https://github.com/danyyacoub/specky), which is already
installed in this environment.

**A change to code that a doc describes is not finished until that doc is updated.** The docs are
read by people who don't read the code, so a doc that describes last month's behaviour is worse than
no doc.

- Search before writing: `specky search "<query>"`, or the `specky` MCP server's `list_features`,
  `list_workflows`, `list_tags` and `get_graph` tools. Don't add a second doc for a topic that
  already has one.
- Write docs in plain language, for a non-technical reader: what it does and why, not how it's
  implemented. Follow the existing docs' shape — `## What It Does`, `## How It Works`, an outcomes
  table, and an `## Acceptance Tests` table of Given/When/Then rows.
- **Before opening a pull request**, run `specky check --base origin/main`. It's offline, needs no
  API key and takes a second; it fails if this branch changed code covered by a doc it didn't touch.
  Fix what it names rather than pushing and waiting for CI to say the same thing.
- `specky pr-comment --base origin/main` prints a markdown summary of the branch's doc changes.
  Include it in the pull request body when the branch touched `specs/`.
- Never hand-write or edit anything under `specs/history/` — those are generated, one per commit.
- Don't commit `specky.toml` or `.specky/`; both are local, and `.specky/index.db` is rebuilt with
  `specky index`.

To document a domain from scratch, follow the procedure in `.devin/document-domain.md`.
