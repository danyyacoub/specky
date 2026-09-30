"""```matrix blocks — scenario tables whose rows are acceptance tests.

A ```matrix``` fence declares typed input columns, typed expected-output
columns, and one row per case, each stating its inputs *and* the outputs a
domain expert expects:

    ```matrix
    name: price_variance
    inputs:
      billed_qty: number
      delivered: number
      status: enum[pending, matched]
    expect:
      qty_var: number
      verdict: enum[conform, to_control]
    formulas:
      qty_var = max(0, billed_qty - coalesce(delivered, billed_qty)) * 32
    ---
    | Over-delivered | 98 | 95 | matched | 96 | to_control |
    ```

The expected values are stated, never computed: a table whose outputs come from
its own formula is confidently wrong whenever the formula is, and the test it
generates would pin that wrong number on the code. `formulas:` is optional and
only *checks* — a formula named after an expected column must reproduce the
stated value on every row, and `specky lint` reports each row where it doesn't
(the rendered table marks the cell too). A formula with any other name is an
intermediate the later ones can use.

Cell values: an empty input cell, or `null`, is a missing value (None); an
empty expected cell is "not asserted on this row"; `null` there expects None.

Where each consumer lives:
- rendering: `render_matrix_blocks` swaps the fence for a static table
  (`<figure class="tw matrix">`), in the viewer, `specky export` and chat
  answers alike; a block that doesn't parse stays as its source text.
- tests: testgen.py writes every block's rows to a JSON file next to the
  scaffold, refreshed on each `specky tests` run, so a doc edit reaches the
  suite without regenerating the hand-wired test.
- lint: lint.py reports blocks that don't parse, and rows whose formulas fail
  or disagree with the stated values.

Like diagram_render.py, this imports nothing but the standard library: the block
is a fence and some arithmetic, and shouldn't pay for markdown/jinja to be one.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass
from decimal import Decimal

# --- the block grammar ---------------------------------------------------------
#
# name:     optional `name: identifier` before the first section — names the
#           generated test and evaluator; blocks without one are numbered.
# inputs:   `name: number | text | bool | enum[a, b, c]`
# expect:   same declarations, for the outputs each row states.
# formulas: optional `name = expression`, evaluated top to bottom; each may use
#           the inputs and the formulas declared before it.
# rows:     pipe table after `---`: the case label, then the input values, then
#           the expected values, each in declaration order.
#
# Expression grammar (recursive descent):
#   or             := and ("or" and)*
#   and            := not ("and" not)*
#   not            := "not" not | comparison
#   comparison     := additive (("==" | "!=" | "<" | "<=" | ">" | ">=") additive)?
#   additive       := multiplicative (("+" | "-") multiplicative)*
#   multiplicative := unary (("*" | "/" | "%") unary)*
#   unary          := "-" unary | primary
#   primary        := number | string | true | false | null | ident
#                   | ident "(" args ")" | "(" or ")"
#
# AST nodes are plain tuples:
#   ("lit", value)  ("var", "x")  ("neg", e)  ("not", e)  ("and", l, r)
#   ("or", l, r)  ("bin", op, l, r)  ("cmp", op, l, r)  ("call", name, args)


class MatrixError(ValueError):
    """Any way a block can be malformed or a row can fail to evaluate — the
    renderer falls back to the fenced source, `specky lint` reports the message."""


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_TOKEN = re.compile(
    r"""
    \s*(?:
        (?P<num>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)
      | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
      | (?P<op>==|!=|<=|>=|[+\-*/%<>(),])
      | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    )
    """,
    re.VERBOSE,
)

_LITERALS = {"true": True, "false": False, "null": None}
_KEYWORDS = {"and", "or", "not"}
_COMPARISONS = ("==", "!=", "<", "<=", ">", ">=")

# The row-cell spellings of a bool. Stated values read as a table, so yes/no
# count too.
_BOOLS = {"true": True, "yes": True, "false": False, "no": False}


@dataclass(frozen=True)
class Column:
    name: str
    kind: str  # number | text | bool | enum
    options: tuple[str, ...] = ()

    def coerce(self, raw: str):
        """A row cell's text as this column's value; `null` is None."""
        raw = raw.strip()
        if raw == "null":
            return None
        if self.kind == "number":
            try:
                return float(raw.replace(",", "").replace("_", ""))
            except ValueError:
                raise MatrixError(f"{self.name}: {raw!r} is not a number") from None
        if self.kind == "bool":
            if raw.lower() not in _BOOLS:
                raise MatrixError(f"{self.name}: {raw!r} is not true/false")
            return _BOOLS[raw.lower()]
        if self.kind == "enum" and raw not in self.options:
            raise MatrixError(
                f"{self.name}: {raw!r} not in enum[{', '.join(self.options)}]"
            )
        return raw


@dataclass(frozen=True)
class Formula:
    name: str
    ast: tuple
    source: str


@dataclass(frozen=True)
class Row:
    label: str
    inputs: tuple  # one value per input column; None when missing
    expected: tuple[tuple[str, object], ...]  # (name, value) for each asserted column


@dataclass(frozen=True)
class Matrix:
    name: str | None
    inputs: tuple[Column, ...]
    expect: tuple[Column, ...]
    formulas: tuple[Formula, ...]
    rows: tuple[Row, ...]


# --- expression tokenizer / parser ---------------------------------------------


def _tokens(source: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(source):
        match = _TOKEN.match(source, pos)
        if not match or match.end() == pos:
            if source[pos:].strip():
                raise MatrixError(
                    f"formula {source!r}: unexpected {source[pos:].strip()[0]!r}"
                )
            break
        pos = match.end()
        kind = match.lastgroup
        out.append((kind, match.group(kind)))
    out.append(("end", ""))
    return out


class _Parser:
    def __init__(self, source: str):
        self.source = source
        self.tokens = _tokens(source)
        self.i = 0

    def _peek(self) -> tuple[str, str]:
        return self.tokens[self.i]

    def _next(self) -> tuple[str, str]:
        token = self.tokens[self.i]
        self.i += 1
        return token

    def _fail(self, message: str) -> MatrixError:
        return MatrixError(f"formula {self.source!r}: {message}")

    def _eat_close(self) -> None:
        if self._next() != ("op", ")"):
            raise self._fail("expected ')'")

    def parse(self) -> tuple:
        node = self._or()
        if self._peek()[0] != "end":
            raise self._fail(f"trailing {self._peek()[1]!r}")
        return node

    def _or(self):
        node = self._and()
        while self._peek() == ("ident", "or"):
            self._next()
            node = ("or", node, self._and())
        return node

    def _and(self):
        node = self._not()
        while self._peek() == ("ident", "and"):
            self._next()
            node = ("and", node, self._not())
        return node

    def _not(self):
        if self._peek() == ("ident", "not"):
            self._next()
            return ("not", self._not())
        return self._comparison()

    def _comparison(self):
        node = self._additive()
        kind, op = self._peek()
        if kind == "op" and op in _COMPARISONS:
            self._next()
            node = ("cmp", op, node, self._additive())
            if self._peek()[0] == "op" and self._peek()[1] in _COMPARISONS:
                raise self._fail("chained comparison — use `and`")
        return node

    def _additive(self):
        node = self._multiplicative()
        while self._peek()[0] == "op" and self._peek()[1] in ("+", "-"):
            op = self._next()[1]
            node = ("bin", op, node, self._multiplicative())
        return node

    def _multiplicative(self):
        node = self._unary()
        while self._peek()[0] == "op" and self._peek()[1] in ("*", "/", "%"):
            op = self._next()[1]
            node = ("bin", op, node, self._unary())
        return node

    def _unary(self):
        if self._peek() == ("op", "-"):
            self._next()
            return ("neg", self._unary())
        return self._primary()

    def _primary(self):
        kind, value = self._next()
        if kind == "num":
            return ("lit", float(value))
        if kind == "str":
            return ("lit", value[1:-1])
        if kind == "ident":
            if self._peek() == ("op", "("):
                if value not in FUNCTIONS:
                    raise self._fail(f"unknown function {value!r}")
                self._next()
                args = []
                if self._peek() != ("op", ")"):
                    args.append(self._or())
                    while self._peek() == ("op", ","):
                        self._next()
                        args.append(self._or())
                self._eat_close()
                return ("call", value, tuple(args))
            if value in _LITERALS:
                return ("lit", _LITERALS[value])
            if value in _KEYWORDS:
                raise self._fail(f"dangling {value!r}")
            return ("var", value)
        if (kind, value) == ("op", "("):
            node = self._or()
            self._eat_close()
            return node
        raise self._fail(f"unexpected {value or 'end of formula'!r}")


def parse_expr(source: str) -> tuple:
    return _Parser(source.strip()).parse()


# --- evaluator -------------------------------------------------------------------


def _num(value, context: str) -> float:
    if value is None:
        raise MatrixError(
            f"{context}: a value is missing — use coalesce() or a null check"
        )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MatrixError(f"{context}: expected a number, got {value!r}")
    return float(value)


def _nums(name: str, values: list, arity: tuple[int, int | None]) -> list[float]:
    low, high = arity
    if len(values) < low or (high is not None and len(values) > high):
        want = (
            f"{low}" if low == high else f"{low}+" if high is None else f"{low}-{high}"
        )
        raise MatrixError(f"{name}() takes {want} argument(s), got {len(values)}")
    return [_num(v, f"{name}()") for v in values]


def _round(values: list) -> float:
    # Half away from zero, the way a spreadsheet or an invoice rounds — Python's
    # round() is banker's rounding and would disagree with the doc on exact halves.
    nums = _nums("round", values, (1, 2))
    factor = 10 ** int(nums[1]) if len(nums) == 2 else 1
    return math.copysign(math.floor(abs(nums[0]) * factor + 0.5) / factor, nums[0])


def _coalesce(values: list):
    if not values:
        raise MatrixError("coalesce() takes 1+ argument(s), got 0")
    return next((v for v in values if v is not None), None)


# name → implementation over already-evaluated arguments. `if` is here for the
# parser's benefit and special-cased in `evaluate_node`, which evaluates only
# the branch it takes. Adding a function is one entry: there is no second
# evaluator to keep in step.
FUNCTIONS = {
    "min": lambda v: min(_nums("min", v, (1, None))),
    "max": lambda v: max(_nums("max", v, (1, None))),
    "abs": lambda v: abs(_nums("abs", v, (1, 1))[0]),
    "round": _round,
    "coalesce": _coalesce,
    "if": None,
}


def _truthy(value) -> bool:
    return bool(value)


def _compare(op: str, left, right) -> bool:
    if op == "==":
        return matches(right, left)
    if op == "!=":
        return not matches(right, left)
    numeric = all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in (left, right)
    )
    textual = all(isinstance(v, str) for v in (left, right))
    if not (numeric or textual):
        raise MatrixError(f"can't order {left!r} {op} {right!r}")
    return {
        "<": left < right,
        "<=": left <= right,
        ">": left > right,
        ">=": left >= right,
    }[op]


def evaluate_node(node, env: dict) -> object:
    tag = node[0]
    if tag == "lit":
        return node[1]
    if tag == "var":
        if node[1] not in env:
            raise MatrixError(f"unknown name {node[1]!r}")
        return env[node[1]]
    if tag == "neg":
        return -_num(evaluate_node(node[1], env), "negation")
    if tag == "not":
        return not _truthy(evaluate_node(node[1], env))
    if tag == "and":
        return _truthy(evaluate_node(node[1], env)) and _truthy(
            evaluate_node(node[2], env)
        )
    if tag == "or":
        return _truthy(evaluate_node(node[1], env)) or _truthy(
            evaluate_node(node[2], env)
        )
    if tag == "bin":
        op = node[1]
        left = _num(evaluate_node(node[2], env), op)
        right = _num(evaluate_node(node[3], env), op)
        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if not right:
            raise MatrixError(f"{op} by zero")
        return left / right if op == "/" else left % right
    if tag == "cmp":
        return _compare(
            node[1], evaluate_node(node[2], env), evaluate_node(node[3], env)
        )
    if tag == "call":
        name, args = node[1], node[2]
        if name == "if":
            if len(args) != 3:
                raise MatrixError("if() takes (condition, then, else)")
            branch = args[1] if _truthy(evaluate_node(args[0], env)) else args[2]
            return evaluate_node(branch, env)
        return FUNCTIONS[name]([evaluate_node(a, env) for a in args])
    raise MatrixError(f"bad expression node {node!r}")


def matches(want, got) -> bool:
    """Whether a computed value is the stated one: numbers to a relative 1e-9
    (float noise, not rounding slack), everything else exactly and by type — a
    bool is never the number 1."""
    if isinstance(want, bool) or isinstance(got, bool):
        return type(want) is type(got) and want == got
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        return math.isclose(want, got, rel_tol=1e-9, abs_tol=1e-9)
    return want == got


def evaluate_row(m: Matrix, row: Row) -> dict[str, object]:
    """Every formula's value for one row, in formula order."""
    env = {col.name: value for col, value in zip(m.inputs, row.inputs)}
    for formula in m.formulas:
        env[formula.name] = evaluate_node(formula.ast, env)
    return {f.name: env[f.name] for f in m.formulas}


def check_row(m: Matrix, row: Row) -> dict[str, object]:
    """The stated values a formula disagrees with on this row, as
    `{column: computed value}`. Raises `MatrixError` if a formula can't evaluate."""
    computed = evaluate_row(m, row)
    return {
        name: computed[name]
        for name, want in row.expected
        if name in computed and not matches(want, computed[name])
    }


# --- block parsing --------------------------------------------------------------


_DECL = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+)$")
_ENUM = re.compile(r"^enum\s*\[(.*)\]\s*$")
_FORMULA_DECL = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$")
_SECTIONS = {"inputs", "expect", "formulas"}
_ROW_SPLIT = re.compile(r"(?<!\\)\|")


def _column(line: str) -> Column:
    match = _DECL.match(line)
    if not match:
        raise MatrixError(f"bad column declaration {line!r}")
    name, spec = match.group(1), match.group(2).strip()
    enum = _ENUM.match(spec)
    if enum:
        options = tuple(o.strip() for o in enum.group(1).split(",") if o.strip())
        if not options:
            raise MatrixError(f"{name}: empty enum")
        return Column(name, "enum", options)
    if spec in ("number", "text", "bool"):
        return Column(name, spec)
    raise MatrixError(f"{name}: unknown type {spec!r} (number, text, bool, enum[…])")


def _cells(line: str) -> list[str]:
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
    return [c.strip().replace("\\|", "|") for c in _ROW_SPLIT.split(body)]


def _row(line: str, inputs: list[Column], expect: list[Column]) -> Row:
    cells = _cells(line)
    label = cells[0] if cells else ""
    want = 1 + len(inputs) + len(expect)
    if len(cells) != want:
        raise MatrixError(
            f"row {label!r}: {len(cells) - 1} value(s), expected {len(inputs)} input(s) "
            f"+ {len(expect)} expected"
        )
    values = cells[1:]
    try:
        row_inputs = tuple(
            None if raw == "" else col.coerce(raw) for col, raw in zip(inputs, values)
        )
        expected = tuple(
            (col.name, col.coerce(raw))
            for col, raw in zip(expect, values[len(inputs) :])
            if raw != ""
        )
    except MatrixError as exc:
        raise MatrixError(f"row {label!r}: {exc}") from None
    return Row(label, row_inputs, expected)


def parse(source: str) -> Matrix:
    """A ```matrix fence's body as a `Matrix`, or `MatrixError`."""
    name = None
    inputs: list[Column] = []
    expect: list[Column] = []
    formulas: list[Formula] = []
    rows: list[Row] = []
    section = None
    for raw in source.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lowered = line.lower()
        if lowered.endswith(":") and lowered[:-1].strip() in _SECTIONS:
            section = lowered[:-1].strip()
            continue
        if line == "---":
            section = "rows"
            continue
        if section is None and lowered.startswith("name:"):
            name = line.split(":", 1)[1].strip()
            if not _IDENT.match(name):
                raise MatrixError(
                    f"name {name!r} must be an identifier (letters, digits, _)"
                )
            continue
        if section == "inputs":
            inputs.append(_column(line))
        elif section == "expect":
            expect.append(_column(line))
        elif section == "formulas":
            match = _FORMULA_DECL.match(line)
            if not match:
                raise MatrixError(f"bad formula {line!r} (want `name = expression`)")
            formulas.append(
                Formula(match.group(1), parse_expr(match.group(2)), match.group(2))
            )
        elif section == "rows" and line.startswith("|"):
            rows.append(_row(line, inputs, expect))
        else:
            raise MatrixError(f"unexpected line {line!r}")
    if not inputs:
        raise MatrixError("block declares no inputs")
    if not expect:
        raise MatrixError(
            "block declares no expected columns — rows must state their outputs"
        )
    columns = [c.name for c in inputs] + [c.name for c in expect]
    if len(columns) != len(set(columns)):
        raise MatrixError("duplicate column name")
    defined = [c.name for c in inputs] + [f.name for f in formulas]
    if len(defined) != len(set(defined)):
        raise MatrixError("a formula redefines an input or an earlier formula")
    return Matrix(name, tuple(inputs), tuple(expect), tuple(formulas), tuple(rows))


FENCE = re.compile(r"^[ \t]*```matrix[ \t]*\n(.*?)^[ \t]*```", re.DOTALL | re.MULTILINE)


def block_sources(markdown: str) -> list[str]:
    """The body of every ```matrix fence in a markdown doc, in order."""
    return FENCE.findall(markdown)


def parse_blocks(markdown: str) -> list[Matrix]:
    """Every well-formed ```matrix block in a markdown doc, in order. Malformed
    ones are skipped — `specky lint` is where they get reported."""
    found = []
    for source in block_sources(markdown):
        try:
            found.append(parse(source))
        except MatrixError:
            continue
    return found


def block_keys(blocks: list[Matrix]) -> list[str]:
    """A distinct identifier per block: its `name:`, else `matrix`, `matrix_2`, …"""
    keys: list[str] = []
    taken = {m.name for m in blocks if m.name}
    unnamed = 0
    for m in blocks:
        if m.name and m.name not in keys:
            keys.append(m.name)
            continue
        while True:
            unnamed += 1
            key = "matrix" if unnamed == 1 else f"matrix_{unnamed}"
            if key not in taken and key not in keys:
                break
        keys.append(key)
    return keys


# --- HTML rendering --------------------------------------------------------------


def fmt(value) -> str:
    """A cell value as the table shows it — never in exponent notation, which
    would hide digits of an amount."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        # Shortest repr after rounding off float noise (0.1 + 0.2), spelled out in full by
        # Decimal so 1e16 still reads as digits.
        text = format(Decimal(repr(round(value, 10))), "f")
        text = text.rstrip("0").rstrip(".") if "." in text else text
        return "0" if text in ("-0", "") else text
    return str(value)


def render_figure(m: Matrix) -> str:
    """One matrix as a static `<figure class="tw matrix">` table: inputs, then the
    stated outputs. A stated value its formula disagrees with is marked, so a
    reader sees the contradiction without running lint."""
    groups = (
        '<tr class="mx-groups"><th></th>'
        f'<th colspan="{len(m.inputs)}">Inputs</th>'
        f'<th class="mx-exp" colspan="{len(m.expect)}">Expected</th></tr>'
    )
    head = ["<th>Case</th>"]
    head += [f"<th>{html.escape(c.name)}</th>" for c in m.inputs]
    head += [
        f'<th class="mx-exp{" mx-first" if i == 0 else ""}">{html.escape(c.name)}</th>'
        for i, c in enumerate(m.expect)
    ]

    body = []
    for row in m.rows:
        try:
            wrong = check_row(m, row) if m.formulas else {}
        except MatrixError:
            wrong = {}  # lint names it; the table still shows what the doc states
        stated = dict(row.expected)
        cells = [f"<td>{html.escape(row.label)}</td>"]
        cells += [f"<td>{html.escape(fmt(v))}</td>" for v in row.inputs]
        for i, col in enumerate(m.expect):
            classes = ["mx-exp"] + (["mx-first"] if i == 0 else [])
            text = html.escape(fmt(stated[col.name])) if col.name in stated else ""
            title = ""
            if col.name in wrong:
                classes.append("mx-bad")
                title = f' title="formula gives {html.escape(fmt(wrong[col.name]))}"'
            cells.append(f'<td class="{" ".join(classes)}"{title}>{text}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")

    caption = ""
    if m.formulas:
        lines = "<br>".join(
            f"<code>{html.escape(f.name)} = {html.escape(f.source)}</code>"
            for f in m.formulas
        )
        caption = f"<figcaption>{lines}</figcaption>"
    return (
        '<figure class="tw matrix"><table><thead>'
        + groups
        + "<tr>"
        + "".join(head)
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
        + caption
        + "</figure>"
    )


_MATRIX_BLOCK = re.compile(
    r'<pre><code class="language-matrix">(.*?)</code></pre>', re.DOTALL
)


def render_matrix_blocks(
    body_html: str, limit: int | None = None
) -> tuple[str, bool, bool]:
    """Replace fenced matrix blocks with `<figure class="tw matrix">` tables.

    Same contract as `diagram_render.render_mermaid_blocks`: `(html,
    any_source, any_rendered)`, `limit` bounds how many fences get rendered
    (chat answers), and a block that fails to parse is left as its source text
    rather than failing the render.
    """
    any_source = False
    any_rendered = False
    drawn = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal any_source, any_rendered, drawn
        any_source = True
        if limit is not None and drawn >= limit:
            return match.group(0)
        drawn += 1
        try:
            figure = render_figure(parse(html.unescape(match.group(1))))
        except MatrixError:
            return match.group(0)
        any_rendered = True
        return figure

    return _MATRIX_BLOCK.sub(repl, body_html), any_source, any_rendered


# The viewer's `figure.tw` already styles the table; this only separates the
# stated outputs from the inputs and flags a stated value its formula disputes.
MATRIX_CSS = """
/* --- matrix blocks: a ```matrix fence as a scenario table (matrix.render_figure). */
figure.matrix tr.mx-groups th {
  font-size: 0.6875rem; text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--text-secondary); font-weight: 600;
}
figure.matrix .mx-first { border-left: 1px dashed var(--border); }
figure.matrix td.mx-exp { font-weight: 600; font-variant-numeric: tabular-nums; }
figure.matrix td.mx-bad {
  color: var(--danger); text-decoration: underline wavy; cursor: help;
}
figure.matrix figcaption {
  padding: 8px 12px; border-top: 1px solid var(--border);
  font-size: 0.75rem; color: var(--text-secondary); line-height: 1.7;
}
"""
