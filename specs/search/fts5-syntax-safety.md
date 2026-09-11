---
type: feature
tags: [search]
---

# Search — FTS5 Syntax Safety

## What It Does

Search queries no longer crash when they contain special characters like `.`, `-`, or `*` that have meaning to the underlying search engine. Instead, these characters are treated as literal text the user searched for, making search behavior predictable and reliable regardless of query content.

## How It Works

1. **User submits a search query** — Any text typed into `specky search` or the chat retrieval box enters the system as-is.

2. **Query is tokenized** — The system extracts individual words from the query, ignoring punctuation and special characters.

3. **Tokens are wrapped in quotes** — Each word is quoted as a literal phrase so that FTS5 engine operators (like `NEAR`, `AND`, `-` for negation, `*` for wildcards) are treated as plain text, not search instructions.

4. **Tokens are combined with OR** — Multiple words are joined with `OR` logic, expanding search results to include documents matching any of the words, not requiring all words to appear.

5. **Safe query is executed** — The processed query runs against the search index without syntax errors.

## Outcomes

| Input | Behavior | Result |
|-------|----------|--------|
| `foo-bar` | Extracted as words "foo" and "bar", searched as `"foo" OR "bar"` | Returns results mentioning either word; `-` treated as text separator, not negation operator |
| `foo.bar` | Extracted as words "foo" and "bar", searched as `"foo" OR "bar"` | Returns results mentioning either word; `.` treated as text, not an operator |
| `foo * bar` | Extracted as words "foo" and "bar", searched as `"foo" OR "bar"` | Returns results mentioning either word; `*` treated as text, not a wildcard |
| Empty query or only punctuation | No tokens extracted | Returns empty result set (no crash) |

## Acceptance Tests

| Given | When | Then |
|-------|------|------|
| A query containing FTS5 operator characters (`.`, `-`, `*`, `NEAR`, `AND`) | User submits search or chat asks a question | Search completes without error and returns documents matching any of the extracted words |
| A multi-word query like `foo bar` | User submits search | Results are broader (OR logic) than before, showing documents with either word, not requiring both |
| An empty query or string with only punctuation | User submits search | System returns no results without crashing |
| A query from the chat retrieval path using the same query builder | Chat asks for context | Both search paths produce identical results and use the same safe query logic |
