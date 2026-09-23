---
type: feature
tags: [ai, cost]
authored: human
sources: [src/specky/ai_provider.py]
---

# Ai — Provider Cost Controls

## What It Does

Three settings that change what specky spends on the AI provider, without changing what it writes.
Every call is split into a stable half that can be cached and a changing half that can't; calls that
depend on nothing but their own input can be sent together at half price; and any one kind of call
can be pointed at a different model from the rest.

They matter because specky asks the same kind of question hundreds of times in a row. The largest
single thing it pays for on a mature repo is not any one doc — it is the list of every doc that
already exists, resent with every commit so the classifier doesn't invent a duplicate.

## How It Works

1. **Every call is split in two** — A prompt has a *prefix*, identical for every call of that kind
   in a run, and a *body* that changes per call. The classification prefix is the instructions plus
   the list of every existing doc; the body is one commit's message and diff.

2. **The prefix is cached by the provider** — With the `anthropic` and `bedrock` providers the
   prefix is sent as a cached system block, billed at a tenth of the input rate after the first call
   (Bedrock bills at its own rates; the request is the same). An
   OpenAI-compatible endpoint receives it as a system message; a `command` provider has one input
   channel, so the two halves are joined back together and nothing is cached by specky.

3. **A prefix below the model's minimum is simply not cached** — It is not an error and carries no
   premium, so a small repo pays exactly what it did before the split existed. The saving arrives
   as the doc set grows, which is when it is needed.

4. **The order is load-bearing** — A prefix cache is a literal prefix match, so anything that varies
   per call has to come after everything that doesn't. A single commit-specific character leaking
   into a prefix turns every call into a cache miss, and nothing reports that it happened.

5. **Independent calls can be batched** — `--batch` on `specky sync` sends
   every commit summary, or every domain's doc, as one Message Batches request at half the
   per-token price. Only calls that depend on nothing but their own input qualify.

6. **Classification is never batched** — It is fed the running list of docs written so far, and
   answering a whole backlog against one frozen list is how a run ends up with three docs about one
   subject. It stays serial and in commit order.

7. **A batch is asynchronous** — specky waits up to an hour, then leaves it running server-side and
   says so. Whatever finished is already memoized, so re-running collects it rather than paying
   again. Anything that goes wrong — no batch API, a failure, a partial result — falls back to
   ordinary calls and prints why.

8. **Each kind of call can name its own model** — `[ai] <task>_model` in `specky.toml` routes one
   task elsewhere; everything else uses `[ai] model`. Tasks: `summary`, `classify`, `doc`,
   `document`, `tag`, `chat` (the Spec Assistant's answers) and `draft` (its draft-spec workflow).
   `specky doctor` prints the routing, because it is otherwise invisible until someone reads a bill.

9. **The environment can set `[ai]`, for a deployed server** — `SPECKY_AI_<KEY>` sets `[ai] <key>`:
   `SPECKY_AI_MODEL`, `SPECKY_AI_DRAFT_MODEL`, `SPECKY_AI_BASE_URL`, `SPECKY_AI_API_KEY_ENV` and so
   on. A container built from the repo has no `specky.toml` (it's gitignored), and a server
   usually wants other models than a laptop, so a host's env settings are where its config
   belongs, as with `SPECKY_AUTH_*`. Without `SPECKY_AI_PROVIDER`, each variable overrides its one
   key of the file. With it, the environment *replaces* the file's `[ai]` table, because the file's
   keys belong to the file's provider: a local `agent = "claude"`, `model = "opus"` carried into a
   server's DeepSeek config would name a model DeepSeek doesn't have. The key itself still goes
   through `api_key_env`, so no secret is ever a `SPECKY_AI_*` value. `specky doctor` names the
   variables in effect.

## Two Different Caches

Easy to confuse, and they compose rather than overlap.

| | Where it lives | How long | What it makes free |
|---|---|---|---|
| **Prompt cache** | The provider (Anthropic) | Minutes | A *similar* call — same prefix, different body |
| **Response cache** | The repo's index (`.specky/index.db`) | Until evicted at 20 MB | An *identical* call — re-running the same work |

The response cache is keyed on model **and** prefix **and** body, so two calls differing only in
their stable half are correctly treated as two different questions. `specky cost` reports its hit
rate; `specky cost --clear-cache` empties it.

## Outcomes

| Condition | Behaviour |
|---|---|
| `anthropic` provider, prefix above the model's minimum | Prefix billed at ~10% of input rate after the first call |
| `anthropic` provider, prefix below the minimum | Nothing cached, nothing extra charged |
| `openai-compatible` provider | Prefix sent as a system message; caching is the endpoint's business |
| `command` provider | Halves rejoined; caching is the command's business, and specky can't measure it |
| `--batch` on a provider with no batch API | Prints that it was ignored, runs normally |
| `--batch` request fails or times out | Prints why, falls back to ordinary calls; nothing is lost |
| `--batch` returns only some answers | The missing ones are generated singly |
| `[ai] <task>_model` names an unknown task | Config error at startup, not a setting that quietly does nothing |
| `[ai] <task>_model` on a `command` provider | Refused — there is no model to swap |
| `SPECKY_AI_DRAFT_MODEL` set over a `specky.toml` | Drafts use that model; every other key comes from the file |
| `SPECKY_AI_PROVIDER` set | The environment is the whole `[ai]` table; the file's `[ai]` is ignored, and no file is needed |
| Neither `specky.toml` nor `SPECKY_AI_PROVIDER` | The usual "run `specky init`" error |
| A git hook fires | Never batches, by design; `git commit` must not become a long poll |

## What It Saves

Classification cost for a ten-commit sync, by doc-set size. Caching does nothing at twelve docs
because the prefix is under the model's minimum; batch halves the bill at every size.

| Doc set | Neither | Caching | Caching + batch |
|---|---|---|---|
| 12 | $0.041 | $0.041 | $0.021 |
| 60 | $0.071 | $0.039 | $0.019 |
| 300 | $0.221 | $0.071 | $0.036 |
| 800 | $0.534 | $0.138 | $0.069 |

A ten-thousand-commit backfill on a three-hundred-doc repo: **$221 with neither, $25 with both.**

## Acceptance Tests

| Scenario | Given | When | Then |
|---|---|---|---|
| Prefix reaches the provider separately | The `anthropic` provider | A call is made with a prefix | It arrives as a cached system block; the body arrives as the user message |
| No prefix, no system block | The `anthropic` provider | A call is made with no prefix | No system block is sent at all |
| A command provider sees one prompt | `provider = "command"` | A call is made with a prefix | The command receives prefix and body joined, exactly as before the split existed |
| The prefix is stable across commits | A run over four commits | Each kind of call is inspected | Every micro-doc prefix is byte-identical, every classification prefix is byte-identical, and there is no third per-commit prefix |
| The prefix is part of the cache key | Two calls with the same body and different prefixes | Both are made | The second is not served from the first's cached answer |
| An identical call is free | The same prefix and body twice | Both are made | The provider is reached once |
| Batch skips what is already cached | One of two entries already memoized | A batch is sent | Only the uncached entry is sent; the memoized answer is returned for the other |
| A batch key with a slash is accepted | A domain keyed `billing/refund-flow` | It is batched | The request id is alphanumeric and within the API's length limit |
| Bedrock has no batch API | A `bedrock` provider | Batch support is asked | No — `--batch` is ignored with a note, as for every non-Anthropic provider; tools are still supported |
| A missing batch API degrades | A `command` provider and `--batch` | `specky sync --batch` | It says the flag was ignored and asks for each summary normally |
| A failed batch degrades | A provider whose batch call raises | `specky sync --batch` | It says the batch failed and asks for each summary singly |
| A per-task model routes only its task | `[ai] model = "haiku"`, `document_model = "sonnet"` | The provider is built | The document task resolves to sonnet; classification and everything else resolve to haiku |
| A task specky no longer has is a config error | `[ai] discovery_model = "sonnet"`, left over from `specky bootstrap` | The provider is built | It fails naming the tasks that exist, rather than routing nothing in silence |
| A misspelled task is caught | `[ai] docs_model = "x"` | The provider is built | A config error naming the valid tasks, not a silent no-op |
| A command provider refuses task models | `provider = "command"` with any `<task>_model` | The provider is built | A config error explaining there is no model to swap |
| One key overridden from the environment | `specky.toml` with `agent = "claude"`, `model = "opus"`; `SPECKY_AI_DRAFT_MODEL=sonnet` | The provider is built | The draft task runs `claude -p --model sonnet`; chat still runs on opus |
| The environment replaces the table | `specky.toml` with an agent config; `SPECKY_AI_PROVIDER=openai-compatible` plus its base URL, model and key variable | The config is read | Only the environment's keys are present — no `agent` or `model` from the file |
| No file, environment only | No `specky.toml`; `SPECKY_AI_PROVIDER=agent`, `SPECKY_AI_AGENT=claude` | The config is read | `provider = "agent"`, `agent = "claude"` |
| A misspelt task from the environment | `SPECKY_AI_DRAFTS_MODEL=sonnet` | The provider is built | The same "names no task" config error as in the file |
| doctor on an environment-only server | No `specky.toml`; `SPECKY_AI_PROVIDER` and its keys set | `specky doctor` | The provider is reported from `SPECKY_AI_PROVIDER` and the variables in effect are named, never their values |
