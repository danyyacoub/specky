"""`specky document` — one feature or workflow, documented from the code by a model that searches.

Everything else in specky is commit-driven: `commit_doc.py` walks `git log` and the only source code
that ever reaches a model is a unified diff. That is the right shape for keeping a doc current,
because a diff is a precise statement of what changed about something already described somewhere.
It is the wrong shape for writing a doc that doesn't exist yet — a feature is not a change, and a
module written once years ago and stable since produces no diff to write from at all.

The predecessor of this module read the whole repo at once: one call to name every domain, then one
call per domain shown a budgeted paste of its files. It produced broad, shallow docs, and the cause
was structural rather than a matter of prompt wording. Whatever Python guesses is relevant is all
the model ever sees, a budget that has to cover an entire repo leaves each feature a few kilobytes,
and a domain the map had no room for is never proposed. So the docs described the files that
happened to fit.

This module inverts both halves:

- **The scope is one thing.** You name a feature or a workflow; nothing else is documented on this
  run. The source budget stops being the binding constraint, because a single feature fits inside
  it with room to spare.
- **The model searches.** It is given tools (`tools.py`) and a turn budget, and it decides what to
  read — so it can follow a call from a route into a service into a helper, which no amount of
  up-front budgeting in Python can do.

What that buys in depth it must pay for in trust, since nothing here can check prose against code.
So the guards are the load-bearing part, and all of them predate this module:

- **Nothing read, nothing written.** The toolbox records every file actually opened; a submission
  backed by none is refused outright. A doc written from no source is fabricated end to end and
  reads exactly like a real one.
- `generator.ungrounded_flags` refuses a doc naming a `--flag` nothing in this repo accepts.
- `generator.lost_content` refuses an update that drops or guts the sections already there.
- A refusal parks the draft in `.specky/pending/` rather than discarding it: it may well be better
  than what is on disk, and that is a judgement for a human.

Nothing is committed, for the same reason `specky sync` doesn't: a generated doc is exactly the
change somebody should read in `git status` first.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from specky import frontmatter, paths, source
from specky.ai_provider import (
    MAX_TOOL_TURNS,
    Provider,
    ToolLoopUnsupported,
    supports_tools,
)
from specky.generator import (
    DOC_STYLE_INSTRUCTIONS,
    WORKFLOW_STYLE_INSTRUCTIONS,
    DocSync,
    ExistingDocs,
    append_glossary_rows,
    doc_problem,
    leading_json_object,
    repair_mermaid,
    stage_pending,
    strip_code_fence,
    update_modules_index,
)
from specky.tools import DocSubmitted, Submission, Toolbox

# How many source paths a doc records in its `sources:` frontmatter. The list is a coverage hint for
# `specky check`, not an inventory: a feature touching 400 files would otherwise write a 400-line
# frontmatter block onto a doc a human has to read.
MAX_SOURCES_RECORDED = 40

# How much of PRODUCT.md and the existing-docs listing the prompt carries. Both are stable across
# runs and so are cached, but a repo with 300 docs would still spend most of a context window
# restating its own index.
PRODUCT_CLIP = 4_000
EXISTING_DOCS_CLIP = 8_000


# Everything identical across every `specky document` run in one repo, so it travels as the cached
# prefix. `CLAUDE.md` is blunt about the cost of getting this wrong: a prefix gets cached only if it
# is byte-identical across calls, and anything per-call leaking in makes every call a cache miss.
# That matters more here than anywhere else in specky — a tool conversation resends its prefix on
# every turn, so on a twelve-turn run this block is read thirteen times and paid for once.
PROCEDURE = (
    """You are writing one reference document about one feature or workflow of this \
codebase, for a reader who is not an engineer. You have tools to search and read the repository. \
Nothing you write reaches disk until you call submit_doc, and specky validates it before it does.

How to work:

1. **Find the code.** Start with search_code, using the words the request uses and the words a user
   of this product would use. Follow the names you find — a route into the service it calls, the
   service into its helpers. Use outline to triage before spending a read.
2. **Read enough to be specific.** A doc is only worth writing if it says what this feature actually
   does: the real statuses, the real thresholds, the real order of steps. Read the code that decides
   those things rather than inferring them from a file name. Read its tests too — a test name is
   often the clearest statement of an expected behaviour in the repo, and its cases are the
   Acceptance Tests you are about to write. They stay readable even when a path scope puts them
   outside it. Where the code raises a question it cannot answer — an oddly specific number, a
   guard that looks redundant, an ordering that seems arbitrary — `history` gives you the commit
   that put it there, which is usually the whole explanation.
3. **Keep `sources` to what the doc is about.** List the one to three files this document actually
   describes — not everything you opened while looking. `sources` is read back as a claim that those
   files are documented here, and a file listed by mistake stops anyone being warned when it changes
   without this doc changing. You may only name files you opened, and a doc backed by no reads at
   all is refused — but that is the floor, not the target.
4. **Check whether it is already documented.** The docs that exist are listed below. If one of them
   already covers this, read it with read_doc and update it in place — answer with that doc's exact
   domain and topic. A second doc on one subject is a defect. When you update, carry the existing
   content through: a rewrite that drops or guts a section is refused outright and the doc is left
   alone.
5. **Stay inside your own subject.** Where an existing doc owns what happens upstream or downstream
   of yours, link to it and describe only your part. Retelling a neighbour's territory is the same
   defect as a duplicate: it reads as authoritative, it is the copy nobody updates, and the two
   drift apart. A doc that says less because its neighbour says the rest is the better doc.
6. **Decide what it is.** A *workflow* is a multi-step process with a sequence and an outcome per
   step. A *feature* is a bounded capability. A single scenario or edge case is a feature. Classify
   by what this document is *about*, not by whether some pipeline exists upstream of it — a set of
   commands for querying data is a feature, even though something had to produce the data.
7. **Write it, then call submit_doc once.**

Naming: `domain` is the module or area, `topic` is the specific subject, both kebab-case. Name them
after what the system does for its users — "billing", "refund-flow", "rate-limits" — never after a
layer: "utils", "helpers", "models", "controllers" and "components" are not domains. The topic is
never "readme".

Ground every claim in code you have read. Where you could not find something, say so in the doc
plainly instead of guessing — a confident sentence about behaviour that does not exist is the one
failure that cannot be spotted by reading the doc.

Both shapes follow — use the one matching the type you chose in step 6, and follow it exactly.

--- If you classified it as a FEATURE ---

"""
    + DOC_STYLE_INSTRUCTIONS.format(domain_title="<Domain>", topic_title="<Topic>")
    + """
--- If you classified it as a WORKFLOW ---

"""
    + WORKFLOW_STYLE_INSTRUCTIONS.format(domain_title="<Domain>", topic_title="<Topic>")
    + """
Also:

- **Acceptance Tests are required.** Concrete scenarios with real-ish values and named entities,
  covering the happy path, at least one edge case, and every threshold or boundary the feature has.
  State the formula inline when a "Then" is a computed number. If there is genuinely nothing
  testable, say so in that section rather than omitting it.
- **Say what would surprise a careful reader.** What deliberately does *not* happen, where the
  behaviour is narrower than its name suggests, what is asymmetric, what is silently ignored. A
  reader can guess the happy path; they cannot guess the exception, and finding it the hard way is
  what the doc exists to prevent. If a rule has a case it pointedly does not cover, say so in the
  same breath as the rule.
- **Diagrams.** A workflow doc always gets one and the workflow template above says where. A
  feature doc gets one only when it earns its space: the logic branches into a real decision tree,
  or distinct actors hand off to each other. A single actor doing a straight sequence does not need
  one — the numbered list already covers it. Use a fenced ```mermaid block: `flowchart` for
  branching, `sequenceDiagram` for actors exchanging steps, never both in one doc. Keep labels short
  and reuse the exact glossary terms, and if the diagram and the numbered steps ever disagree, the
  steps win.
- **No frontmatter.** Start the markdown at its `# ` heading. The frontmatter is rendered from the
  fields you pass to submit_doc.
- **Vocabulary.** Reuse the glossary terms below exactly; do not invent a synonym for a concept that
  already has one. Pass `glossary_terms` only for genuinely new shared vocabulary other docs will
  reuse — not for terms local to this one doc.
"""
)


def _section(title: str, body: str) -> str:
    return f"\n\n{title}\n{body}" if body.strip() else ""


def build_prefix(repo_root: Path, existing: ExistingDocs, scope: str | None = None) -> str:
    """The cacheable half: the procedure, this repo's framing, its vocabulary and its shape."""
    product = ""
    product_path = paths.product_doc(repo_root)
    if product_path.exists():
        product = source.clip(product_path.read_text().strip(), PRODUCT_CLIP)

    from specky.html_render import load_glossary

    glossary = load_glossary(repo_root)
    terms = "\n".join(f"- {term}: {definition}" for term, definition in sorted(glossary.items()))

    return (
        PROCEDURE
        + _section(
            "--- What this product is (the framing every doc is written against) ---",
            product
            or "(no PRODUCT.md in this repo — write for a reader who does not already know what "
            "this product is for)",
        )
        + _section("--- Shared vocabulary, reuse these exactly ---", terms or "(none yet)")
        + _section(
            "--- Docs that already exist (domain/topic — what each covers) ---",
            source.clip(existing.docs_block(), EXISTING_DOCS_CLIP),
        )
        + _section("--- Tags already in use, prefer one of these ---", existing.tags_line())
        + _section(
            "--- Where code lives in this repo (directory: source files, total size) ---",
            source.tree(repo_root, scope) or "(no tracked source files)",
        )
    )


def build_prompt(
    request: str,
    domain: str | None = None,
    topic: str | None = None,
    max_turns: int = MAX_TOOL_TURNS,
) -> str:
    """The volatile half — strictly everything that changes per run, and nothing else.

    The turn budget lives here rather than in the procedure because it is a per-run number, and a
    prefix is cached only while it is byte-identical. It is worth telling the model at all because
    a budget it cannot see is one it cannot spend well: left to guess, a thorough model reads two
    modules in full and is cut off before it writes anything.
    """
    lines = [
        f"Document this feature or workflow: {request}",
        f"You have at most {max_turns} turns. Spend roughly the first half finding and reading "
        "the code, and leave yourself room to write — a run that never calls submit_doc produces "
        "nothing.",
    ]
    if domain:
        lines.append(f"It belongs in domain `{domain}` — pass exactly that as `domain`.")
    if topic:
        lines.append(f"Its topic slug is `{topic}` — pass exactly that as `topic`.")
    return "\n".join(lines)


# --- the degraded path for providers with no tool channel -----------------------------------------

DEGRADED_SUFFIX = """
You have no tools from specky on this run. Use whatever file-reading and search tools your own
environment gives you to find and read the relevant code before answering. If you have none, say so
in the "markdown" field rather than describing code you have not seen.

Respond with ONLY a JSON object, no other text:

{"domain": "kebab-case", "topic": "kebab-case", "type": "feature|workflow",
 "tags": ["tag"], "purpose": "one line for an index table",
 "sources": ["src/billing/refund.py"], "markdown": "the complete doc, starting at its # heading",
 "glossary_terms": [{"term": "Refund window", "definition": "one or two sentences"}]}
"""


def _degraded(repo_root: Path, prefix: str, prompt: str, provider: Provider) -> Submission | None:
    """One call, no tools, a JSON envelope back.

    For `provider = "command"`, which is one stdin and one stdout. The command is often an agent
    that has perfectly good tools of its own — `claude -p` can read and grep — they are just
    invisible to specky, which can neither offer them nor see what was opened. So this path keeps
    the same prompt and the same validation, and loses exactly one thing: the guarantee that the doc
    was written from code somebody actually read. That is why it warns.
    """
    raw = provider.generate(prompt + DEGRADED_SUFFIX, prefix=prefix, task="document")
    data = leading_json_object(strip_code_fence(raw))
    if data is None:
        return None
    return Submission(
        domain=str(data.get("domain", "")).strip(),
        topic=str(data.get("topic", "")).strip(),
        doc_type=data.get("type") if data.get("type") in ("feature", "workflow") else "feature",
        tags=[t.strip() for t in data.get("tags") or [] if isinstance(t, str) and t.strip()],
        purpose=str(data.get("purpose", "")).strip(),
        sources=[s.lstrip("./") for s in data.get("sources") or [] if isinstance(s, str)],
        markdown=str(data.get("markdown") or ""),
        glossary_terms=[t for t in data.get("glossary_terms") or [] if isinstance(t, dict)],
    )


# --- writing what came back -----------------------------------------------------------------------


def _slug(text: str) -> str:
    """kebab-case, and never a path. A domain becomes a directory name and a topic becomes a
    filename, so a `/` or a `..` the model emitted has to be flattened before either is joined to
    the docs root."""
    from specky.adopt import kebab

    return kebab(str(text).replace("/", "-"))


def meta_stub(submission: Submission) -> dict:
    """Frontmatter for a draft that is being parked rather than written.

    Deliberately not the full block `write` renders: a parked draft has no `sources:` claim worth
    recording (nothing has validated it) and must not inherit the hand-written keys off the doc it
    was refused in favour of, which are that doc's, not this draft's.
    """
    return {"type": submission.doc_type, "tags": submission.tags}


@dataclass(frozen=True)
class Written:
    """What one run produced, beyond the doc itself."""

    doc: DocSync
    glossary: Path | None = None
    glossary_terms: int = 0

    @property
    def paths(self) -> list[Path]:
        return ([self.doc.path] if self.doc.written else []) + (
            [self.glossary] if self.glossary else []
        )


def _refused(doc_path: Path, note: str) -> Written:
    """A refusal, in the one shape all of them share.

    Five call sites in `write`, each of which was a six-line nested constructor. Collapsing them
    makes the refusals visibly the same kind of thing, which is the point — they are one policy
    ("say why, write nothing"), not five decisions.
    """
    return Written(DocSync(doc_path, False, note))


def _recorded_sources(
    repo_root: Path, submission: Submission, read: list[str], require_reads: bool
) -> list[str]:
    """Which files this doc may claim to be about.

    `sources:` is what gives a doc `specky check` coverage at all: `indexer.declared_sources` folds
    these pairs into the code-to-doc map, which is otherwise derived from git log alone and cannot
    see a doc written from code it did not change. It is also weighted *above* the git-derived
    pairs, so a file named here by mistake is a file nobody is warned about when it changes.

    Two regimes, because the evidence differs. With tools, the claim is intersected with what was
    actually opened, so a file the model merely saw in a search result cannot claim coverage; no
    claim at all falls back to the read set, which is the more useful answer and is evidence either
    way. Without tools there is no read set to check against, so the claim is taken on trust,
    filtered only to paths that really are tracked source — which at least stops a doc claiming
    coverage of a file that does not exist.
    """
    if require_reads:
        return ([path for path in submission.sources if path in read] or read)[
            :MAX_SOURCES_RECORDED
        ]
    tracked = set(source.source_files(repo_root))
    return [path for path in submission.sources if path in tracked][:MAX_SOURCES_RECORDED]


def write(
    repo_root: Path,
    submission: Submission,
    read: list[str],
    existing: ExistingDocs,
    *,
    require_reads: bool = True,
) -> Written:
    """Validate a submission and put it on disk, or refuse it and say why.

    `generator.doc_problem` holds the guards this shares with the commit path, so the two refuse the
    same things in the same order. What is added here is everything that path gets for free: a doc
    must name a domain and topic, must have a body, and must be written from source somebody
    actually opened. On the commit path the diff *is* the source; here the model chose what to read,
    so it has to be checked.
    """
    domain, topic = _slug(submission.domain), _slug(submission.topic)
    docs_root = paths.docs_root(repo_root)
    if not domain or not topic:
        # No doc path to name yet, so the docs root stands in for one. `DocSync.path` means "the
        # doc this is about", and there isn't one — but `Written.paths` skips it either way.
        return _refused(docs_root, "refused — the submission named no domain/topic")

    doc_path = docs_root / domain / f"{topic}.md"
    rel = doc_path.relative_to(repo_root)

    # Strip any frontmatter the model produced: the real block is rendered below from the fields it
    # passed to submit_doc, and a second one would land on top of it.
    _, body = frontmatter.parse(strip_code_fence(submission.markdown))
    body = body.strip() + "\n"
    # Before every other guard, because it rewrites the body the rest are measured against — and
    # because a class suffix naming nothing is the one diagram defect specky can fix outright.
    body, repairs = repair_mermaid(body)
    if not body.strip():
        return _refused(doc_path, f"refused {rel} — the submission had no markdown")
    if require_reads and not read:
        return _refused(
            doc_path, f"refused {rel} — no source file was read, so the doc has nothing behind it"
        )

    sources = _recorded_sources(repo_root, submission, read, require_reads)

    existing_meta: dict = {}
    existing_body = None
    if doc_path.exists():
        existing_meta, existing_body = frontmatter.parse(doc_path.read_text())
        # A doc somebody took ownership of is not something an automatic run gets to rewrite. The
        # commit path can check this before generating anything; here it cannot, because the model
        # chooses the target — so by the time we know, the doc has been written and paid for.
        # Parking it is therefore the same courtesy every other refusal extends: the draft may well
        # be worth reading next to the one on disk, and that is a judgement for its owner.
        if str(existing_meta.get("authored", "")).strip().lower() == "human":
            pending = stage_pending(
                repo_root, domain, topic, frontmatter.render(meta_stub(submission), body)
            )
            return _refused(
                doc_path,
                f"left {rel} alone (authored: human) — may need a look. The draft it wrote "
                f"is at {pending.relative_to(repo_root)}",
            )

    meta: dict[str, str | list[str]] = {"type": submission.doc_type, "tags": submission.tags}
    if sources:
        meta["sources"] = sources
    # Hand-authored keys survive a regeneration, exactly as they do in `generator.sync_feature_doc`:
    # the model is never asked for any of them, so without this an update would silently strip an
    # `owner:` somebody added yesterday.
    for key in ("related", "owner", "authored", "origin"):
        if existing_meta.get(key):
            meta[key] = existing_meta[key]
    content = frontmatter.render(meta, body)

    problem = doc_problem(repo_root, existing_body, body)
    if problem:
        pending = stage_pending(repo_root, domain, topic, content)
        return _refused(
            doc_path, f"refused {rel} — {problem}. Draft kept at {pending.relative_to(repo_root)}"
        )

    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(content)
    update_modules_index(repo_root, domain, f"{domain}/{topic}.md", submission.purpose)
    existing.record(f"{domain}/{topic}", submission.purpose, submission.tags)

    glossary_path, added = append_glossary_rows(repo_root, submission.glossary_terms)
    verb = "updated" if existing_body else "wrote"
    note = f"{verb} {rel}"
    if repairs:
        note += f" (repaired its diagram: {', '.join(sorted(set(repairs)))})"
    return Written(DocSync(doc_path, True, note), glossary_path, added)


# --- orchestration ----------------------------------------------------------------------------------


def _confirm(request: str, max_turns: int, assume_yes: bool) -> None:
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise RuntimeError(
            f"documenting {request!r} costs up to {max_turns} AI calls and stdin isn't a terminal "
            "— re-run with --yes"
        )
    answer = input(
        f"specky document: {request!r} — up to {max_turns} AI calls. Continue? [y/N] "
    )
    if answer.strip().lower() not in ("y", "yes"):
        raise RuntimeError("cancelled")


def document(
    repo_root: Path,
    request: str,
    provider: Provider,
    *,
    domain: str | None = None,
    topic: str | None = None,
    scope: str | None = None,
    max_turns: int = MAX_TOOL_TURNS,
    assume_yes: bool = False,
    dry_run: bool = False,
    label: str = "specky document",
) -> list[Path]:
    """Search this repo for `request`, write its doc, and return what landed on disk.

    Idempotent in the sense that matters: run it again on the same feature and the model is shown
    the doc that now exists and told to update it in place rather than write a twin. There is no
    state anywhere — `.specky/` is gitignored, so anything kept there would vanish on a fresh clone,
    and the docs tree is the only record a re-run needs.
    """
    toolbox = Toolbox(repo_root, scope)
    if not toolbox.allowed:
        print(f"{label}: no tracked source files{f' under {scope}' if scope else ''}")
        return []

    existing = ExistingDocs.load(repo_root)
    prefix = build_prefix(repo_root, existing, scope)
    prompt = build_prompt(request, domain, topic, max_turns)

    if dry_run:
        # Unlike its predecessor's dry run, this one calls nothing at all — there is no discovery
        # step to pay for, so there is no reason for a preview to cost anything.
        print(f"{label}: would send this prompt and then run up to {max_turns} turns\n")
        print(prefix)
        print(f"\n--- the per-run half ---\n{prompt}")
        return []

    _confirm(request, max_turns, assume_yes)

    tool_capable = supports_tools(provider, "document")
    if not tool_capable:
        print(
            f"{label}: this provider has no tool channel, so specky can't watch what gets read — "
            "the doc will be written from whatever the command finds on its own, and `sources:` "
            "is taken on trust. Point [ai] at provider = \"anthropic\" for the checked path."
        )

    submission: Submission | None = None
    if tool_capable:
        try:
            said = provider.converse(
                prompt,
                prefix=prefix,
                tools=toolbox.tools(),
                invoke=_announce(toolbox, label),
                task="document",
                max_turns=max_turns,
                final_tool="submit_doc",
            )
        except DocSubmitted as submitted:
            submission = submitted.submission
        except ToolLoopUnsupported:
            # Only reachable if `supports_tools` and the provider disagree — belt and braces, since
            # the cost of being wrong is an unhandled traceback at the end of a paid-for run.
            # `tool_capable` has to come down with it: no tool ran, so the read set is empty, and
            # leaving it True would send `write` down the `require_reads` path to a refusal that
            # could never be satisfied — turning the safety net into a guaranteed dead end.
            tool_capable = False
            submission = _degraded(repo_root, prefix, prompt, provider)
        else:
            # Nothing submitted, but the turns were still paid for — and what a model says on its
            # way out is usually the doc itself, written as prose instead of handed over. Parking
            # it costs nothing and is the difference between a wasted run and one somebody can
            # finish by hand. `specky doctor` warns while it sits there.
            note = ""
            if said.strip():
                parked = stage_pending(repo_root, "unsubmitted", _slug(request) or "doc", said)
                note = f". What it said is kept at {parked.relative_to(repo_root)}"
            print(
                f"{label}: the model stopped after {len(toolbox.calls)} tool call(s) without "
                f"submitting a doc{note}"
            )
            return []
    else:
        submission = _degraded(repo_root, prefix, prompt, provider)

    if submission is None:
        print(f"{label}: could not read a doc out of the response")
        return []

    result = write(repo_root, submission, toolbox.read, existing, require_reads=tool_capable)
    print(f"{label}: {result.doc.note}")
    if result.doc.written and tool_capable:
        # Only where it means something. On the degraded path both counts are zero because specky
        # saw nothing, not because nothing was read — printing that would read as the opposite.
        print(f"{label}: read {len(toolbox.read)} file(s) over {len(toolbox.calls)} tool call(s)")
    if result.glossary:
        print(
            f"{label}: {result.glossary_terms} term(s) into "
            f"{result.glossary.relative_to(repo_root)}"
        )
    return result.paths


def _announce(toolbox: Toolbox, label: str):
    """Wrap `Toolbox.invoke` so each call prints as it happens.

    A twelve-turn conversation takes minutes. Every other long-running command in specky prints per
    unit of work — one line per commit in `sync`, one per file in `adopt` — and a silent wait here
    reads as a hang.
    """

    def invoke(name: str, arguments: dict) -> str:
        result = toolbox.invoke(name, arguments)
        print(f"{label}:   {toolbox.calls[-1]}")
        return result

    return invoke
