"""The facts a doc states — numbers, identifiers, formulas, defined terms — and which ones a rewrite lost.

`generator.lost_content` measures how much of a section survived a rewrite, in characters. That
catches a section being gutted, and misses the loss that matters most in a functional doc: the
allocation score that used to read `+1000 within tolerance, else 500 − |Δ%| × 5` coming back as
"lines are scored on price and quantity". Same length, same headings, every constant gone. A
migration in a real repo lost dozens of those that way — scoring weights, a `variance_pct`
formula, a field-precedence table — and nothing measured it, because nothing looked below the
paragraph.

So this module looks at the things a reader can act on and a summary can't reproduce:

- **number** — a threshold, weight or limit: a decimal, a percentage, anything ≥ 10, or a number
  next to an operator (`+1`, `× 5`, `≤ 0.10`). Small bare numbers are prose ("two steps") far more
  often than they are rules, and numbers in example tables (Acceptance Tests) are illustrations —
  both are left out, because a report nobody can read past its noise isn't a report.
- **identifier** — a backticked span, or a snake_case/dotted name inside a code block: the field,
  setting or status a reader would grep for.
- **formula** — a code-block line with `=`, `≤`, `≥` or `→`. Reported as one fact rather than as
  the numbers and names inside it, and only when one of those went missing: a formula whose every
  term still appears in the new text has been restated, not lost.
- **definition** — a bold first cell in a table row (`| **Tolerance** | … |`), which is how a
  glossary defines a term. Present only if the new text still defines it the same way; a term that
  merely gets *mentioned* afterwards is exactly the drift a glossary is meant to stop.
- **term** — any other bold span that reads like a name rather than emphasis.

Deterministic, offline, no AI call: it runs in `specky check` on every pull request and in
`specky adopt --verify` over a whole tree, so it has to be free, fast and repeatable. It can't tell
a legitimately changed threshold from a lost one — the old value is absent either way — so
everything built on it reports and never refuses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

from specky import frontmatter
from specky.testgen import cells

# Sections whose numbers are worked examples rather than rules. Their identifiers still count — a
# scenario naming `effective_total` is evidence the field exists — but "invoice 100 @ €33" being
# rewritten as "invoice 50 @ €12" is not a fact going missing.
EXAMPLE_SECTIONS = ("acceptance test", "example", "scenario")

# How many facts a one-line summary names before it says "and N more".
SUMMARY_LIMIT = 5

_FENCE = re.compile(r"^\s*(```+|~~~+)\s*([\w+-]*)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BACKTICK = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+?)\*\*")
# Link targets carry numbers (issue ids, file names) that are addresses, not facts.
_LINK_TARGET = re.compile(r"\]\([^)\s]*\)")
_LIST_MARKER = re.compile(r"^\s*(?:\d+[.)]|[-*+])\s+")
# `\d[\d,]*` rather than a strict thousands pattern, so "5,000" is one number; a run that isn't a
# valid grouping ("1,2,3") is split back into its parts in `_numbers`.
_NUMBER = re.compile(r"(?<![\w.])(\d[\d,]*)(?:\.(\d+))?(\s?%)?")
_OPERATORS = set("+-−×*/<>≤≥=~")
_CODE_NAME = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+|[A-Za-z0-9]*_[A-Za-z0-9_]+)")
# A relation, not an arrow: `→` in a code block is a decision tree's "then" far more often than
# it is a rule, so it doesn't make a line a formula (its numbers are still facts).
# `=` counts only spaced (`variance_pct = …`) or ending a line that continues below (`f(x) =`):
# unspaced it's an attribute (`documents={docs}`, `source_type=EMAIL`), and `=>` is an arrow.
_FORMULA = re.compile(r"\s=(?![=>])(?:\s|$)|[≤≥]|\s[<>]=\s")
# Tree-drawing characters from ASCII decision trees, which would otherwise be read as content.
_BOX_DRAWING = re.compile(r"[│├└┌┐┘┬┴┼─━┃]+")
# A backticked file path. The doc's `sources:` frontmatter is where files belong, and a rewrite that
# stops naming `api/common/discounts.py` in prose has lost nothing a reader acts on.
_PATH = re.compile(r"^[\w.-]*/[\w./-]*$|^[\w.-]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|md|sql|json|ya?ml|toml|sh|go|rs|java|kt|rb|php|cs|swift|html|css)$")
# What makes a backticked span a formula rather than a name: a symbol only arithmetic uses
# (`price × quantity`, `base − discount`, `Σ share_i`), or a plainer operator between spaced operands
# when a code-like name or a number is there too (`discount + global_discount`). The second half is
# strict because `/` and `+` are also punctuation: "N articles attendus / M reçus" is a message.
_MATH_SYMBOL = re.compile(r"[×−≤≥Σ]|\s=\s")
_PLAIN_OPERATOR = re.compile(r"\s[*/+<>-]\s")
_CODE_OR_NUMBER = re.compile(r"\w_\w|\d")

# Words that make a bold span read as emphasis ("**never** retried", "**only** the first") rather
# than as the name of something.
# Words a formula is written *with* rather than about.
_FORMULA_WORDS = frozenset("and or not when otherwise else then where max min abs sum".split())

_STOPWORDS = frozenset(
    "a an and any are at be by each every for from if in is it its no not of on only or the this "
    "that to was when with without never always all none once".split()
)


@dataclass(frozen=True)
class Fact:
    kind: str  # number | identifier | formula | definition | term
    key: str  # as written, for display
    line: str  # the line it came from, for context
    section: str = ""  # the `##` heading it sits under ("" for the preamble)
    # A formula's own numbers and names — what has to survive for it to count as restated.
    parts: tuple["Fact", ...] = ()
    # Whether this is a *constant* — a number, a defined term, or a formula that carries a number
    # or defines a quantity the doc names in prose — rather than a *name*. Reports lead with
    # constants and list names compactly: a lost scoring weight is the finding, a lost helper
    # name is context.
    constant: bool = False

    @property
    def norm(self) -> str:
        if self.kind == "number":
            return _number_value(self.key)
        if self.kind in ("definition", "term"):
            return term_key(self.key)
        return " ".join(self.key.split())

    @property
    def context_words(self) -> frozenset[str]:
        """The words around a number that say what it's a number *of* — see `Index.states_near`."""
        return frozenset(
            w for w in re.findall(r"[a-z_]{4,}", self.line.lower()) if w not in _STOPWORDS
        )

    def label(self) -> str:
        if self.kind == "number":
            return self.key
        if self.kind == "formula":
            return f"`{_clip(self.key, 80)}`"
        if self.kind == "identifier":
            return f"`{self.key}`"
        return f"**{self.key}**"

    def as_dict(self) -> dict:
        return {"kind": self.kind, "key": self.key, "line": self.line, "section": self.section}


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _number_value(raw: str) -> str:
    """`5,000` → `5000`, `0.10` → `0.1`, `5 %` → `5`: the value, however it was typeset.

    A percentage compares by its number alone. "5%" rewritten as "5 percent" is the same rule, and
    a report that flagged it would teach its reader to skim.
    """
    digits = raw.replace(",", "").replace("%", "").strip()
    try:
        return format(Decimal(digits).normalize(), "f")
    except InvalidOperation:
        return digits


def term_key(term: str) -> str:
    """How two spellings of one term compare: case, spacing and a plural on the last word ignored.

    Only the last word, and only the two regular endings: "Entry matches" and "entry match" are
    one term, while a real inflection ("analysis"/"analyses") is left to a human. A stemmer here
    would merge terms that happen to share a stem, which is worse than missing a plural.
    """
    words = term.lower().split()
    if not words:
        return ""
    last = words[-1]
    if len(last) > 4 and last.endswith("ies"):
        last = last[:-3] + "y"
    elif len(last) > 3 and last.endswith("s") and not last.endswith("ss"):
        last = last[:-1]
    return " ".join([*words[:-1], last])


def is_term_candidate(span: str) -> bool:
    """Does this bold span name something, rather than stress a word?

    Up to five words, not ending in punctuation (a bold lead-in sentence "**Nothing is committed.**"
    is a label, not a term), and either capitalised or a multi-word phrase with no filler words. A
    single lowercase word is emphasis ("**never**"), and so is any phrase carrying a stopword
    ("**in the detail view only**").
    """
    span = span.strip()
    words = span.split()
    if not words or len(words) > 5 or len(span) < 3:
        return False
    if span[-1] in ".:;,!?—-" or span.startswith("`"):
        return False
    if not any(ch.isalpha() for ch in span):
        return False
    lowered = [w.lower().strip("()") for w in words]
    if span[0].isupper():
        # Capitalised is how a doc names a status or verdict ("To control", "Conform"), stopwords
        # and all — but a lone capitalised stopword is still emphasis ("**Never**").
        return len(words) <= 4 and not (len(words) == 1 and lowered[0] in _STOPWORDS)
    return len(words) > 1 and not any(w in _STOPWORDS for w in lowered)


def _is_table_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def _numbers(text: str) -> list[tuple[str, bool, int]]:
    """Every number in `text` as `(raw, operator-adjacent, offset)`, list markers excepted."""
    found = []
    skip_until = -1
    marker = _LIST_MARKER.match(text)
    if marker and marker.group(0).strip()[:-1].isdigit():
        skip_until = marker.end()
    for match in _NUMBER.finditer(text):
        if match.start() < skip_until:
            continue
        intpart, decimals, percent = match.group(1).rstrip(","), match.group(2), match.group(3) or ""
        if not intpart:
            continue
        before = text[: match.start()].rstrip()
        adjacent = bool(before) and before[-1] in _OPERATORS
        if "," in intpart and not re.fullmatch(r"\d{1,3}(?:,\d{3})+", intpart):
            # "1,2,3" is a list, not one number. Its parts are rarely rules, but keep them honest.
            found += [(part, False, match.start()) for part in intpart.split(",") if part]
            continue
        raw = intpart + (f".{decimals}" if decimals else "") + percent.strip()
        found.append((raw, adjacent, match.start()))
    return found


def significant_numbers(text: str) -> list[tuple[int, str]]:
    """`(offset, raw)` for every number in `text` that `extract` would call a fact."""
    return [(at, raw) for raw, adjacent, at in _numbers(text) if _significant(raw, adjacent)]


def number_value(raw: str) -> str:
    return _number_value(raw)


def is_example_section(section: str) -> bool:
    return _is_example(section)


def _significant(raw: str, adjacent: bool) -> bool:
    if "." in raw or raw.endswith("%") or adjacent:
        return True
    try:
        return Decimal(raw.replace(",", "")) >= 10
    except InvalidOperation:
        return False


def _prose_facts(line: str, section: str, example: bool) -> list[Fact]:
    facts: list[Fact] = []
    context = _clip(line.strip(), 160)
    scrubbed = _LINK_TARGET.sub("]", line)

    for span in _BACKTICK.findall(scrubbed):
        span = span.strip()
        if re.fullmatch(r"[\d.,]+%?", span):
            continue  # a number in backticks is still a number — `_numbers` below sees it
        if len(span) < 3 or not any(ch.isalnum() for ch in span) or _PATH.match(span):
            continue
        if _MATH_SYMBOL.search(span) or (_PLAIN_OPERATOR.search(span) and _CODE_OR_NUMBER.search(span)):
            facts.append(_formula(span, context, section, example, constant=True))
        else:
            facts.append(Fact("identifier", span, context, section))

    table = _is_table_row(line)
    first_cell = cells(line)[0] if table and cells(line) else ""
    label_zone = _LIST_MARKER.match(scrubbed)
    label_start = label_zone.end() if label_zone else len(scrubbed) - len(scrubbed.lstrip())
    for match in _BOLD.finditer(scrubbed):
        span = match.group(1).strip()
        if table and first_cell == f"**{span}**":
            facts.append(Fact("definition", span, context, section, constant=True))
        elif not table and match.start() == label_start:
            continue  # a step's or paragraph's bold lead-in is a label, not a term
        elif is_term_candidate(span):
            facts.append(Fact("term", span, context, section))

    if not example:
        without_code = _BACKTICK.sub(lambda m: m.group(0) if re.fullmatch(r"`[\d.,]+%?`", m.group(0)) else " ", scrubbed)
        for raw, adjacent, _ in _numbers(without_code.replace("`", " ")):
            if _significant(raw, adjacent):
                facts.append(Fact("number", raw, context, section, constant=True))
    return facts


def _code_facts(line: str, section: str, example: bool) -> list[Fact]:
    """A code-block line's facts: its numbers, or — when it states a relation — one formula.

    Names inside a code block are never facts on their own. The blocks an older doc carries are
    as often pseudo-code (`phase1_allocate(...)`, `sort_desc_by`) as formulas, and a rewrite that
    explains the algorithm instead of transcribing it has lost nothing a reader needs; flagging
    every helper name it dropped buried the scoring weights in one real migration report under a
    hundred of them. A name only counts here as part of a formula, which is lost when one of its
    parts is.
    """
    text = _BOX_DRAWING.sub(" ", line).strip()
    if not text:
        return []
    context = _clip(text, 160)
    if _FORMULA.search(text) and any(ch.isalpha() for ch in text):
        return [_formula(text, context, section, example)]
    if example:
        return []
    return [
        Fact("number", raw, context, section, constant=True)
        for raw, adjacent, _ in _numbers(text)
        if _significant(raw, adjacent)
    ]


def _formula(text: str, context: str, section: str, example: bool, constant: bool = False) -> Fact:
    """One formula fact, carrying the names and numbers it has to keep to count as restated.

    A formula with a number in it is a constant outright. One without is a constant only when
    `extract` later finds its left-hand side named in the doc's prose (`variance_pct = …` in a doc
    that talks about `variance_pct`): that is a quantity the doc defines, where `qty = min(…)` in a
    block of pseudo-code is a step of an algorithm.
    """
    names = [Fact("identifier", name, context, section) for name in _CODE_NAME.findall(text)]
    # Plain words are parts too (`final = base − discount` names three things), compared as terms —
    # case-insensitively, either grammatical number — since prose restating a formula capitalises
    # and inflects them freely.
    coded = {part for name in names for part in re.split(r"[_.]", name.key)}
    words = [
        Fact("term", word, context, section)
        for word in dict.fromkeys(re.findall(r"(?<![\w.])[A-Za-z][A-Za-z]{2,}(?![\w(])", text))
        if word.lower() not in _FORMULA_WORDS and word not in coded
    ]
    numbers = [
        Fact("number", raw, context, section, constant=True)
        for raw, adjacent, _ in _numbers(text)
        if _significant(raw, adjacent) and not example
    ]
    return Fact(
        "formula", text, context, section, tuple(names + words + numbers), constant or bool(numbers)
    )


def extract(text: str) -> list[Fact]:
    """The facts in a doc, in reading order, each `(kind, normalised key)` once.

    Frontmatter is dropped (it's metadata, and `sources:` would be a wall of identifiers), and so
    are ```mermaid``` blocks: a diagram restates the steps above it, so anything only it says is a
    label, and the steps are what `lost_content` and the reader both treat as the source of truth.
    """
    _, body = frontmatter.parse(text)
    facts: list[Fact] = []
    seen: set[tuple[str, str]] = set()
    section = ""
    fence: str | None = None  # the fence's language while inside one, "" for a bare fence
    fence_marker = ""
    block: list[str] = []  # the current code block's lines
    block_facts: list[Fact] = []

    def keep(found: list[Fact]) -> None:
        for fact in found:
            key = (fact.kind, fact.norm)
            if fact.norm and key not in seen:
                seen.add(key)
                facts.append(fact)

    for line in body.splitlines():
        opened = _FENCE.match(line)
        if fence is not None:
            if opened and opened.group(1).startswith(fence_marker[:3]) and not opened.group(2):
                keep(_settle_block(block, block_facts))
                fence, block, block_facts = None, [], []
                continue
            if fence != "mermaid":
                block.append(line)
                block_facts += _code_facts(line, section, _is_example(section))
            continue
        if opened:
            fence, fence_marker = opened.group(2).lower(), opened.group(1)
            continue
        heading = _HEADING.match(line)
        if heading:
            if len(heading.group(1)) == 2:
                section = heading.group(2).strip()
            continue
        keep(_prose_facts(line, section, _is_example(section)))
    keep(_settle_block(block, block_facts))  # an unclosed fence still said something

    named = {fact.key for fact in facts if fact.kind == "identifier"}
    return [
        replace(fact, constant=True)
        if fact.kind == "formula" and not fact.constant and _defined_name(fact.key) in named
        else fact
        for fact in facts
    ]


# How a line of pseudo-code starts. A block with any of these is an algorithm written out, whose
# assignments are steps (`qty = min(…)`); a block with none is a list of formulas, every one of them
# a rule the doc states (`variance_pct = (Σ target − Σ source) / …`).
_PSEUDO_CODE = re.compile(
    r"^\s*(?:for|if|elif|else|while|skip|emit|return|yield|def|let|const|var|await|async|break|"
    r"continue|match|case|try|except|raise|\|>|#|//)\b"
)


def _settle_block(lines: list[str], found: list[Fact]) -> list[Fact]:
    if any(_PSEUDO_CODE.match(line) for line in lines):
        return found
    return [replace(f, constant=True) if f.kind == "formula" else f for f in found]


def _defined_name(formula: str) -> str:
    """`variance_pct = (…)` → `variance_pct`; anything whose left side isn't one bare name → ""."""
    left, sep, _ = formula.partition("=")
    left = left.strip()
    return left if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", left) else ""


def _is_example(section: str) -> bool:
    lowered = section.lower()
    return any(word in lowered for word in EXAMPLE_SECTIONS)


class Index:
    """One text, prepared so asking "does it still state this?" is cheap.

    `specky adopt --verify` asks it for every fact of every old doc against every new doc, so the
    work that doesn't depend on the fact — the number set, the bold spans, a whitespace-collapsed
    copy — is done once here.
    """

    def __init__(self, text: str) -> None:
        _, body = frontmatter.parse(text)
        self.text = " ".join(body.split())
        self.lower = self.text.lower()
        # Per line: its numbers and its words, for `states_near`. A worked example's numbers don't
        # count as stating anything, for the same reason `extract` doesn't take facts from one: a
        # weight of 100 is not kept by a scenario that happens to bill 100 units.
        self.lines = []
        section = ""
        for line in _LINK_TARGET.sub("]", body).splitlines():
            heading = _HEADING.match(line)
            if heading and len(heading.group(1)) == 2:
                section = heading.group(2)
            if _is_example(section):
                continue
            self.lines.append(
                (
                    {_number_value(raw) for raw, _, _ in _numbers(line)},
                    set(re.findall(r"[a-z_]{4,}", line.lower())),
                )
            )
        self.numbers = {number for numbers, _ in self.lines for number in numbers}
        self.definitions = set()
        self.bold = set()
        for line in body.splitlines():
            if _is_table_row(line):
                row = cells(line)
                if row and row[0].startswith("**") and row[0].endswith("**"):
                    self.definitions.add(term_key(row[0].strip("*")))
            self.bold.update(term_key(span) for span in _BOLD.findall(line))

    def _has_text(self, key: str, *, case: bool) -> bool:
        haystack, needle = (self.text, key) if case else (self.lower, key.lower())
        needle = " ".join(needle.split())
        if needle not in haystack:
            return False
        # Whole-word only where the key starts/ends with a word character: `total` must not be
        # found inside `effective_total`.
        left = r"(?<![\w])" if needle[:1].isalnum() or needle[:1] == "_" else ""
        right = r"(?![\w])" if needle[-1:].isalnum() or needle[-1:] == "_" else ""
        return re.search(left + re.escape(needle) + right, haystack) is not None

    def mentions(self, term: str) -> bool:
        """Does the text use `term` — bold or not, any case, either grammatical number?"""
        return self._has_term(term)

    def _has_term(self, key: str) -> bool:
        if term_key(key) in self.bold:
            return True
        # Either grammatical number counts: "Regular entry" defined, "regular entries" written.
        return any(self._has_text(form, case=False) for form in _number_forms(key))

    def has(self, fact: Fact) -> bool:
        if fact.kind == "number":
            return fact.norm in self.numbers
        if fact.kind == "identifier":
            return self._has_text(fact.key, case=True)
        if fact.kind == "definition":
            return fact.norm in self.definitions
        if fact.kind == "term":
            return self._has_term(fact.key)
        # A formula survives when everything it says survives, in whatever layout: a code block
        # turned into a sentence is a restatement. With nothing to check it falls back to its text.
        if fact.parts:
            return all(self.has(part) for part in fact.parts)
        return self._has_text(fact.key, case=False)


def _number_forms(term: str) -> list[str]:
    """`term` as written, singular and plural — the spellings a mention of it can take."""
    singular = term_key(term)
    head, _, last = singular.rpartition(" ")
    prefix = f"{head} " if head else ""
    plural = last[:-1] + "ies" if last.endswith("y") and last[-2:-1] not in "aeiou" else last + "s"
    return list(dict.fromkeys([term.lower(), singular, prefix + plural]))


def states_near(index: Index, fact: Fact) -> bool:
    """Does `index` state this number *about the same thing* — on a line sharing a word with it?

    The test `locate` uses for numbers, where `Index.has` would be far too generous: a doc about
    supplier quotes mentioning "1000 cm" does not state an allocation weight of 1000, and a report
    that said the weight had "moved" there would hide the one finding it exists to make. The
    counterpart doc keeps the lenient test — it's the same topic, so the same value almost always is
    the same fact.
    """
    words = fact.context_words
    return any(
        fact.norm in numbers and (not words or words & line_words)
        for numbers, line_words in index.lines
    )


def missing_from(facts: list[Fact], index: Index) -> list[Fact]:
    return [fact for fact in facts if not index.has(fact)]


def dropped(old: str, new: str) -> list[Fact]:
    """Facts `old` states that `new` doesn't, in `old`'s reading order."""
    return missing_from(extract(old), Index(new))


def locate(fact: Fact, indexes: dict[str, Index]) -> str | None:
    """The first of `indexes` (by key order) that still states `fact`, if any.

    Numbers — alone, or as a formula's parts — must sit next to a word of their own context
    (`states_near`): a bare value turns up in some unrelated doc of any real tree.
    """
    for path, index in indexes.items():
        if _located(fact, index):
            return path
    return None


def _located(fact: Fact, index: Index) -> bool:
    if fact.kind == "number":
        return states_near(index, fact)
    if fact.kind == "formula" and fact.parts:
        return all(_located(part, index) for part in fact.parts)
    return index.has(fact)


def summary(facts: list[Fact], limit: int = SUMMARY_LIMIT) -> str:
    """`` `variance_pct`, 5000, **Tolerance** and 4 more `` — one line for a hook note."""
    shown = ", ".join(fact.label() for fact in facts[:limit])
    more = len(facts) - limit
    return f"{shown} and {more} more" if more > 0 else shown
