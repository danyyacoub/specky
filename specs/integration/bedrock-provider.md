---
type: feature
tags: [ai, configuration, security]
---

# Integration — Bedrock Provider

## What It Does
Specky can write docs and answer questions using Claude models hosted on Amazon Bedrock, without an Anthropic API key. AWS teams authenticate with the credentials they already have — an IAM role on a server, an instance profile on EC2, or a named profile on a laptop. Prompt caching and the tool loop behave the same as the existing Anthropic provider, because both share the same request code.

## How It Works
1. **Opt in to the SDK** — the AWS signing libraries ship as an optional extra, so people who never touch AWS don't install boto3; the CLI is installed with `specky[bedrock]`.
2. **Select the provider** — the provider is set to `bedrock` (by `specky init --provider bedrock` or an environment variable on a server).
3. **Name the models** — a Bedrock model ID is chosen for answers, and optionally a stronger one for drafts.
4. **Set a region or profile only if needed** — a region or named profile is supplied when the machine's AWS config doesn't already carry one.
5. **Let the AWS SDK find credentials** — specky never stores them; the SDK resolves a task role, pod role, instance profile, environment keys, or a mounted profile.
6. **Send requests through the shared Anthropic path** — request signing goes through Bedrock, while the caching and tool-loop behaviour is identical to the Anthropic provider.
7. **Validate with a real call** — `init` makes one small test call to prove the provider works, unless skipped; `specky doctor` reports the provider, its source, whether the SDK is installed, and whether a region is set, but makes no AI call.

## Outcomes
| Situation | Result |
|---|---|
| `bedrock` extra not installed | Requests can't be signed; the provider is unavailable until the extra is installed. |
| AWS SDK finds credentials | Requests proceed using the resolved role, profile, or environment keys. |
| No credentials or no region found | The request fails; credentials and model access are first truly tested by the first question asked. |
| Named model not enabled in the region | The call is rejected by Bedrock; the model must be enabled in the console and allowed for the role. |
| Batch job requested | Not supported — Bedrock has no Message Batches API, so batch is missing relative to the Anthropic provider. |
| `specky doctor` run in a container | Reports provider, its source, SDK presence, and region; makes no AI call. |

## Acceptance Tests
| Given | When | Then |
|---|---|---|
| Specky is installed without the `bedrock` extra | The `bedrock` provider is selected | Signing is unavailable; the extra must be installed first. |
| The `bedrock` extra is installed and valid credentials are present | A question is asked in the Spec Assistant panel | The answer is generated through Bedrock with no Anthropic key. |
| A named model is not enabled in the chosen region | A request targets that model | Bedrock rejects the call. |
| The server has no `specky.toml` | `SPECKY_AI_PROVIDER=bedrock` and model variables are set | The environment is the whole `[ai]` table; nothing from a local config leaks in. |
| Caching or tool-loop behaviour is exercised via Bedrock | Requests are made | Behaviour matches the Anthropic provider, since the request code is shared. |
| A batch operation is attempted on this provider | The batch path is invoked | It is unavailable, because Bedrock exposes no Message Batches API. |
| `specky doctor` is run in a container | No AI call is made | It reports the provider, its source, and for Bedrock whether the SDK is installed and a region is set. |
