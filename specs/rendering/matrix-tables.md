---
type: feature
tags: [rendering, cli]
sources:
  - src/specky/matrix.py
  - src/specky/testgen.py
  - src/specky/lint.py
  - src/specky/doc_tools.py
  - src/specky/html_render.py
  - src/specky/answer_render.py
  - src/specky/export.py
---

# Rendering — Matrix Tables

## What It Does

A ` ```matrix ` fenced block states a calculation or decision rule as cases: typed input columns, typed expected-output columns, and one row per case giving its inputs and the outputs a domain expert expects. Every renderer shows it as a table. `specky lint` checks each row against the block's optional formulas, and `specky tests` turns each block into a parametrized test over the rows the doc states, so a rule-heavy doc (variances, pricing, matching verdicts, scoring) is pinned to the code case by case.

The expected values are written down, never computed. A table whose outputs come from its own formula is confidently wrong whenever the formula is, and the test it generated would pin that wrong number on the code. Formulas are a second opinion that lint compares against the stated values.

## How It Works

1. **Declare** — an optional `name:` names the block's generated test. `inputs:` and `expect:` list typed columns (`number`, `text`, `bool`, `enum[a, b]`). An optional `formulas:` list of `name = expression` lines follows. After `---`, each pipe-table row is the case label, the input values, then the expected values, in declaration order.
2. **Parse** — `matrix.parse` checks the declarations and coerces every cell (numbers may use `,` or `_` separators; bools read true/false/yes/no). A wrong cell count, a value outside its enum, or an unknown type is a block error. An empty input cell, or `null`, is a missing value; an empty expected cell means the row doesn't assert that column.
3. **Check** — a formula named after an expected column must reproduce the stated value on every row (numbers to a relative 1e-9). Any other formula is an intermediate the later ones can use. Expressions support arithmetic, comparisons, `and`/`or`/`not`, `true`/`false`/`null`, and `min`, `max`, `abs`, `round` (half away from zero), `if` and `coalesce`. Arithmetic on a missing value, division by zero, or ordering unlike types is an error rather than a guess.
4. **Render** — the viewer, `specky export` and Spec Assistant answers replace the fence with a static table: an Inputs group, an Expected group, and the formulas as a caption. A stated value its formula disputes is underlined, with the computed value as its tooltip. There is no JavaScript. A block that fails to parse stays as its own fenced source.
5. **Lint** — `specky lint` reports a block that doesn't parse, a row whose formula can't evaluate, and each stated value a formula disputes.
6. **Test** — `specky tests` writes one parametrized, skipped test per block into the doc's scaffold, calling an `evaluate_<name>(inputs)` stub the project wires to its code. The rows live in `tests/spec/test_<slug>.matrix.json`, which every run rewrites, so a doc edit reaches an already-wired test without `--force`. `specky tests --check` exits 1 when that data is behind the docs.
7. **Behaviours** — the Spec Assistant's `doc_behaviours` lists each matrix row under Acceptance Tests as an `AT-n` behaviour, with its inputs and expected outputs.

## Example

```matrix
name: price_variance
inputs:
  ordered: number
  delivered: number
  billed_qty: number
  billed_price: number
  ref_price: number
expect:
  price_var: number
  qty_var: number
  verdict: enum[conform, to_control]
formulas:
  price_var = (billed_price - ref_price) * billed_qty
  qty_var = max(0, billed_qty - coalesce(delivered, ordered)) * ref_price
  verdict = if(price_var + qty_var > 0, "to_control", "conform")
---
| Two-component over-billing | 100 | 95 | 98 | 33 | 32 | 98 | 96 | to_control |
| Conform | 100 | 100 | 100 | 32 | 32 | 0 | 0 | conform |
| No delivery document | 100 | | 110 | 32 | 32 | 0 | 320 | to_control |
```

## Outcomes

| Result | Meaning |
|--------|---------|
| Rendered | Valid block → static table in the viewer, export/PDF and chat answers |
| Disputed | A formula disagrees with a stated value → the cell is underlined and `specky lint` names the row |
| Left as source | Malformed block → fenced text; `specky lint` names the problem |
| Scoped | Spec Assistant answers render at most `MAX_ANSWER_MATRICES` (4) blocks; extras stay as source |

## Acceptance Tests

| Scenario | Given | When | Then (expected) |
|---|---|---|---|
| Rendered | A doc with a valid ` ```matrix ` block | `specky render-html` or `specky export` runs | A table with Inputs and Expected column groups and the formulas as a caption; no form controls |
| Disputed value | A row states `price_var` 95 but the formula gives 98 | `specky lint` runs | One matrix problem naming the row, the stated value and the computed one; the rendered cell is underlined |
| Malformed | Block declares `x: bogus` | `specky lint` runs | A matrix problem is reported; the doc page keeps the source text |
| Formula error | Formula uses an undeclared name, or does arithmetic on an empty input cell | `specky render-html` and `specky lint` run | The page renders the stated values unmarked; lint reports the row |
| No expected columns | Block has `inputs:` and `formulas:` but no `expect:` | `specky lint` runs | Reported as malformed — rows must state their outputs |
| Large amount | A row states `1234567.89` | Page renders | Cell shows `1234567.89`, not exponent notation |
| Tests | Doc has a named matrix under `## Acceptance Tests` | `specky tests` runs | The scaffold gets `evaluate_<name>` and `test_<name>` parametrized over the rows in `test_<slug>.matrix.json` |
| Doc edit | The scaffold exists and a row's expected value changes | `specky tests` runs without `--force` | Scaffold untouched; the `.matrix.json` rewritten with the new value |
| CI check | The `.matrix.json` is behind the doc | `specky tests --check` runs | Nothing written; the file named; exit 1 |
