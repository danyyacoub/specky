"""`facts.py` — what a doc states that a summary can't reproduce, and which of it a rewrite lost.

The fixtures are shaped like the losses that motivated it: a real migration rewrote an allocation
score (`+1000 / 500 − Δ% × 5 / 100 − Δq × 10`) into prose, dropped a `variance_pct` formula and a
field-precedence table, and let two dozen glossary terms fall out — all while every section kept
its heading and most of its length.
"""

from __future__ import annotations

from specky import facts

SCORING = """---
type: feature
tags: [matching]
sources: [api/match_service.py]
---

# Matching — Line items

## How It Works

1. **Allocate** — pair lines that share an item id.
2. **Score** — each pair gets a score; higher is better.

```
score(s, t) = price_component + qty_component
price_component:
    within tolerance     → +1000
    otherwise            → +max(0, 500 − |price_diff_pct| × 5)
qty_component:
    +max(0, 100 − |t.qty − s.qty| × 10)
```

The share is `allocated_quantity / quantity`, stored in `allocated_quantity`.

```mermaid
flowchart LR
  A[Score 9999] --> B
```

## Acceptance Tests

| Scenario | Given | When | Then |
|---|---|---|---|
| Overbilled | PO 100 @ €32, invoice 100 @ €33 | variance | +€100 on `price_variance` |
"""

GLOSSARY = """# Glossary

| Term | Definition |
|---|---|
| **Tolerance** | Per-organisation price thresholds. |
| **Price variance** | `(invoice price − reference price) × quantity`. |
| **Regular entry** | A line for actual goods. |
"""


def kinds(found: list[facts.Fact]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for fact in found:
        out.setdefault(fact.kind, set()).add(fact.key)
    return out


class TestExtract:
    def test_scoring_weights_are_numbers_and_constants(self):
        found = facts.extract(SCORING)
        numbers = {f.key for f in found if f.kind == "number"}
        assert {"1000", "500", "100"} <= numbers
        assert all(f.constant for f in found if f.kind == "number")

    def test_list_markers_and_small_bare_numbers_are_not_facts(self):
        found = facts.extract("# T\n\n1. **Step** — two steps then 3 more.\n2. Next one.\n")
        assert not [f for f in found if f.kind == "number"]

    def test_example_sections_contribute_names_but_not_numbers(self):
        found = kinds(facts.extract(SCORING))
        assert "32" not in found.get("number", set())
        assert "price_variance" in found["identifier"]

    def test_mermaid_and_frontmatter_are_skipped(self):
        found = kinds(facts.extract(SCORING))
        assert "9999" not in found.get("number", set())
        assert "api/match_service.py" not in found.get("identifier", set())

    def test_step_labels_are_not_terms(self):
        found = kinds(facts.extract(SCORING))
        assert "Allocate" not in found.get("term", set())

    def test_glossary_rows_are_definitions(self):
        found = kinds(facts.extract(GLOSSARY))
        assert found["definition"] == {"Tolerance", "Price variance", "Regular entry"}

    def test_backticked_arithmetic_is_a_constant_formula(self):
        found = facts.extract(GLOSSARY)
        formula = next(f for f in found if f.kind == "formula")
        assert formula.key == "(invoice price − reference price) × quantity"
        assert formula.constant

    def test_file_paths_are_not_identifiers(self):
        found = kinds(facts.extract("# T\n\nLives in `api/common/discounts.py` and `schemas.py`.\n"))
        assert not found.get("identifier")

    def test_pseudo_code_assignments_are_names_but_formula_blocks_are_constants(self):
        pseudo = "# T\n\n```\nfor s in pairs:\n    qty = min(a, b)\n```\n"
        formulas = "# T\n\n```\nvariance_pct = (target − source) / source × 100\n```\n"
        assert not facts.extract(pseudo)[0].constant
        assert facts.extract(formulas)[0].constant

    def test_a_formula_defining_a_name_the_prose_uses_is_a_constant(self):
        text = "# T\n\nWe report `ratio`.\n\n```\nfor x in y:\n    ratio = a / b\n```\n"
        formula = next(f for f in facts.extract(text) if f.kind == "formula")
        assert formula.constant

    def test_attributes_and_arrows_are_not_formulas(self):
        text = "# T\n\n```\n<Viewer documents={docs} />\nonLoad?: () => void;\n```\n"
        assert not [f for f in facts.extract(text) if f.kind == "formula"]

    def test_emphasis_is_not_a_term(self):
        text = "# T\n\nIt is **never** retried, and **in the detail view only**. Status **To control**.\n"
        assert kinds(facts.extract(text)).get("term") == {"To control"}


class TestDropped:
    def test_a_prose_rewrite_loses_the_weights(self):
        rewrite = SCORING.replace(
            SCORING[SCORING.index("```\nscore") : SCORING.index("The share")],
            "Pairs are scored on price and quantity.\n\n",
        )
        lost = {f.key for f in facts.dropped(SCORING, rewrite)}
        assert {"1000", "500", "100"} <= lost

    def test_restating_a_formula_in_prose_keeps_it(self):
        old = "# T\n\n```\nfinal = base − discount\n```\n"
        new = "# T\n\nThe `final` amount is `base` minus the `discount`, e.g. `base − discount`.\n"
        assert not [f for f in facts.dropped(old, new) if f.kind == "formula"]

    def test_numbers_compare_by_value(self):
        assert not facts.dropped("# T\n\nA gap of 5,000 or ≤ 0.10.\n", "# T\n\nUp to 5000, or ≤ 0.1.\n")

    def test_a_substring_is_not_the_same_number_or_name(self):
        lost = {f.key for f in facts.dropped("# T\n\nCap is 100 on `total`.\n", "# T\n\nCap is 1000 on `effective_total`.\n")}
        assert lost == {"100", "total"}

    def test_a_definition_survives_only_as_a_definition(self):
        mentioned = "# Glossary\n\n| Term | Definition |\n|---|---|\n| **Price variance** | See tolerance. |\n| **Regular entry** | x |\n"
        lost = {f.key for f in facts.dropped(GLOSSARY, mentioned) if f.kind == "definition"}
        assert lost == {"Tolerance"}

    def test_terms_match_either_grammatical_number(self):
        old = "# T\n\nOnly **Regular entry** lines count.\n"
        assert not facts.dropped(old, "# T\n\nOnly regular entries count.\n")


class TestLocate:
    def test_a_bare_number_elsewhere_is_not_the_same_fact(self):
        fact = next(f for f in facts.extract(SCORING) if f.key == "1000")
        elsewhere = {"quotes.md": facts.Index("# Q\n\nQuote B: 1000 cm of cable.\n")}
        assert facts.locate(fact, elsewhere) is None

    def test_a_number_next_to_its_context_is_located(self):
        fact = next(f for f in facts.extract(SCORING) if f.key == "1000")
        owner = {"scoring.md": facts.Index("# S\n\nA price within tolerance scores +1000.\n")}
        assert facts.locate(fact, owner) == "scoring.md"


def test_summary_names_a_few_and_counts_the_rest():
    found = [f for f in facts.extract(GLOSSARY) if f.kind == "definition"]
    assert facts.summary(found, limit=2) == "**Tolerance**, **Price variance** and 1 more"
