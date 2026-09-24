from specky import frontmatter


def test_no_frontmatter_returns_text_unchanged():
    text = "# Title\n\nBody.\n"
    assert frontmatter.parse(text) == ({}, text)


def test_parses_scalars_and_lists():
    meta, body = frontmatter.parse(
        "---\ntype: feature\ntags: [billing, refunds]\n---\n\n# Title\n"
    )
    assert meta == {"type": "feature", "tags": ["billing", "refunds"]}
    assert body == "# Title\n"  # the blank line after the closing fence belongs to the fence


def test_round_trip():
    meta = {"type": "workflow", "tags": ["a", "b"], "related": ["x/y"]}
    body = "# Title\n\nBody.\n"
    parsed_meta, parsed_body = frontmatter.parse(frontmatter.render(meta, body))
    assert parsed_meta == meta
    assert parsed_body == body


def test_render_without_meta_returns_body():
    assert frontmatter.render({}, "# Title\n") == "# Title\n"


def test_empty_list_value():
    meta, _ = frontmatter.parse("---\ntags: []\n---\nbody\n")
    assert meta == {"tags": []}


def test_horizontal_rule_is_not_frontmatter():
    """A doc opening with a thematic break used to be parsed as frontmatter, swallowing the
    body into `meta` — the second `---` closed a block that never opened."""
    text = "---\n\nSome prose that happens to follow a rule.\n\n---\n\nMore prose.\n"
    meta, body = frontmatter.parse(text)
    assert meta == {}
    assert body == text


def test_prose_lines_without_colon_are_not_frontmatter():
    text = "---\nthis is not a key value pair\n---\nbody\n"
    meta, body = frontmatter.parse(text)
    assert meta == {}
    assert body == text


def test_a_yaml_block_list_is_read_as_a_list():
    """Agents write `sources:` as a block list as often as inline; the whole block used to be
    dropped, losing the doc's `type` and `tags` with it."""
    text = "---\ntype: feature\ntags: [discounts]\nsources:\n  - api/a.py\n  - 'api/b.py'\nowner:\n---\n\n# Body\n"
    meta, body = frontmatter.parse(text)
    assert meta == {
        "type": "feature",
        "tags": ["discounts"],
        "sources": ["api/a.py", "api/b.py"],
        "owner": "",
    }
    assert body == "# Body\n"


def test_a_list_item_with_no_key_above_it_is_not_frontmatter():
    text = "---\n- not a key\n---\nbody\n"
    assert frontmatter.parse(text) == ({}, text)
