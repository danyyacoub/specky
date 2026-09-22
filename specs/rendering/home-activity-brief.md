---
type: feature
tags: [rendering, documentation]
related: [documentation/auto-commit-docs, rendering/html-viewer-shell]
sources: [src/specky/activity.py, src/specky/html_render.py, src/specky/commit_doc.py]
---

# Rendering — Home Activity Brief

## What It Does

The viewer's home page has a **Recent activity** section: who changed what on the team's mainline
over the last two weeks. It's written for a product owner who wants the shape of the work, not its
commits. There's one collapsed row per person, with how much they shipped, what's still in
progress, and the features they touched. Opening a row lists each change in plain language.

Three things decide what appears:

- **One pull request is one change.** A branch's in-between commits ("wip", "address review") are
  never entries of their own. They're folded into the merge that brought them in.
- **The words come from the history docs**, not from commit messages. Each line is a history doc's
  headline ([documentation/auto-commit-docs.md](../documentation/auto-commit-docs.md)). Commits
  whose `impact` is `internal` are counted ("+2 internal") rather than listed.
- **People, not agents.** Coding agents and bots are not people here: Claude co-author trailers,
  Copilot, Cursor, `[bot]` accounts, and specky's own doc-sync commits all drop out.

No model is called to build it; it is git and the committed docs only.

## How It Works

1. **Pick the mainline** — `[activity] branch` if set. Otherwise `origin/dev`, then
   `origin/develop`, then `origin/HEAD`, then `HEAD`. In a gitflow repo, `dev` is where work lands
   and `main` only receives release merges. The section header names the branch it walked.
2. **Walk what landed** — `git log --first-parent` on the mainline over the window. Each
   first-parent commit is one change:
   - A **merge** covers its whole branch. The commits it brought in (`M^1..M^2`) give it its people
     and its lines, oldest first. The PR title from the merge message (GitHub, GitLab or Bitbucket
     format) is its label, linked to the PR when `origin` is on GitHub or GitLab.
   - A **commit with one parent** (a direct push or a squash) is a change of its own. A trailing
     `(#N)` gives it a PR label.

   The window is about when a change *landed*. A branch commit written a month ago and merged
   yesterday counts.
3. **Walk what hasn't landed** — commits on remote branches (local ones when there's no remote)
   that the mainline can't reach yet, from the window. They are grouped by branch and shown as
   In progress. Long-lived branches (`main`, `master`, `dev`, `develop`, `trunk`) are never
   in-progress work.
4. **Read the words** — each commit's history doc:
   - **Source:** the working-tree doc, the same file the viewer renders as that commit's page, so
     the line and the page it links to always agree. Failing that, the doc on the commit's own
     branch, which is the only place an unmerged branch's docs exist.
   - **Line text:** the headline, or the first sentence of a legacy doc.
   - **No doc:** the commit's subject, in italics. Those commits are counted in the footer, which
     points at `specky sync`.
5. **Find the people** — a change's people are the humans among its authors and `Co-authored-by`
   trailers:
   - **Agents and bots are dropped.** They are recognised by address rather than by name, because
     Claude and Devin are also people's first names.
   - **A branch no human wrote** goes to whoever merged it. With no human at all, the change is
     only counted, as automated.
   - **Two addresses under one name are one person.** `.mailmap` is the override.
6. **Chips** — the feature and workflow docs a change touched. The strongest evidence comes first:
   the history docs' `features:`, then feature docs the change edited, and only when neither says
   anything, the two docs that cover its code most strongly.
7. **Render** — people appear by most recent activity, each row closed until clicked:
   - Within a person, changes made only of `internal` commits sink below the rest.
   - Lists past 15 items and changes with more than 3 lines finish with a native "+N more"
     disclosure, which needs no script.
   - Dates are absolute, with an "as of" date in the header, because the site is static.

## Configuration

`[activity]` in `specky.toml`, or `[tool.specky.activity]` in `pyproject.toml`:

| Key | Default | Meaning |
|---|---|---|
| `branch` | auto-detected | The mainline to walk; a name that isn't a revision leaves the section out with a message, rather than showing another branch under this name |
| `days` | `14` | How far back the window reaches |
| `ignore_authors` | `[]` | Extra fnmatch patterns, matched against `Name <email>`, for identities that aren't people (a CI user, a team's own bot) |
| `enabled` | `true` | `false` leaves the section out |

## Outcomes

| Condition | Result |
|---|---|
| A branch of three commits by two people is merged | One change listed under both, labelled with the PR title, with three lines from the commits' history docs |
| A commit is co-authored by Claude | It is credited to the human alone |
| Dependabot or Renovate commits land | They appear in no one's list; the footer counts them as automated |
| A commit has no history doc | Its subject is shown in italics and counted in the footer |
| A change's commits are all `impact: internal` | It is listed after the person's user-facing changes, as "+N internal" |
| A branch is pushed but not merged | It is listed under its authors as In progress, in the words of the docs committed on it |
| The checkout is a shallow clone (a depth-1 CI checkout) | The section says so and asks for full history, instead of showing a partial picture |
| Nothing landed in the window | The section says so and names the branch |
| `[activity] branch` names a branch that doesn't exist | The section is left out and the render prints why; the rest of the site renders |

## Acceptance Tests

| Given | When | Then |
|---|---|---|
| A branch with commits by Alice and Bob, each with a history doc, merged by Carol with `--no-ff` | The site is rendered | Alice and Bob each have one shipped change with three lines, labelled with the PR title; Carol isn't listed |
| A direct commit whose message has `Co-authored-by: Claude Opus 5 <noreply@anthropic.com>` | The site is rendered | The commit is under its human author, and the home page doesn't contain "Claude" |
| A commit by `dependabot[bot]` | The brief is collected | No one is credited, and the automated count is 1 |
| Every work commit followed by specky's doc-sync commit | The brief is collected | No line is a doc-sync commit, and a merge's commit count excludes them |
| A commit with no history doc | The brief is collected | Its line is its subject, with no history link, and the undocumented count is 1 |
| A legacy history doc "Refunds are capped. They used to be unlimited." | The brief is collected | The line is "Refunds are capped." |
| A history doc rewritten in the working tree but not committed | The brief is collected | The line is the rewritten headline |
| An unmerged branch whose history docs are committed only on it | The brief is collected from `main` | Its author has it In progress, with the headlines from those docs |
| A remote with `main` and `feat/export`, and `origin/HEAD` → `main` | The brief is collected | The header says `origin/main`; `origin/feat/export` is In progress; `main` never is |
| A commit dated 30 days ago on a branch merged today, and a direct commit from 30 days ago | The brief is collected with a 14-day window | The branch commit is listed, and the old direct commit is not |
| Two commits by "Alice Martin" from two addresses | The brief is collected | There is one Alice Martin, with two changes |
| `[activity] branch = "dev"` | The brief is collected | `dev` is walked and named in the header |
| A `--depth 1` clone | The brief is collected | It reports shallow history instead of any people |
