---
type: feature
tags: [ai, diagnostics, cli]
---

# Diagnostics — Provider Command Errors

## What It Does
When specky sends a request to an external agent command and that command fails, specky reports why it failed instead of only showing a generic non-zero exit status. Separately, `specky doctor` checks that the Devin command-line tool is logged in when Devin is the configured provider, since a logged-out state causes every Devin call to fail.

## How It Works
1. **Run the provider command** — Specky runs the configured agent command with the prompt as input.
2. **Detect a failure** — If the command exits with a non-zero status, specky treats it as a failure rather than returning whatever it printed.
3. **Attach the reason** — The failure message includes the last few lines the command wrote to its error or standard output, so a concrete reason such as "Login canceled" is surfaced instead of just an exit code.
4. **Preserve compatibility** — The failure is still raised as the standard process-error type, so any existing callers that already handle that error keep working unchanged.
5. **Check Devin login** — When the provider command is `devin` and the tool is on the path, `specky doctor` runs a local login-status check and reports whether Devin CLI is logged in. Inside a Devin Desktop session a logged-out CLI only warns: that session's own commits hand off to the skills and never call `devin -p`, so the login matters only for commits made from a terminal or CI.

## Outcomes
| Situation | Result |
| --- | --- |
| Provider command succeeds | Output is returned as before |
| Provider command exits non-zero | Failure raised with the command, exit code, and the last few lines of its output |
| Devin is the provider and Devin CLI is logged in | Doctor reports OK, "Devin CLI is logged in" |
| Devin is the provider and Devin CLI is not logged in | Doctor reports FAIL, "Devin CLI is not logged in — run `devin auth login`" |
| Same, but doctor runs inside a Devin Desktop session | Doctor reports WARN: session commits hand off to the skills, terminal/CI commits need `devin auth login` |
| Devin is not the provider, or Devin CLI is not on the path | No Devin login check is added |

## Acceptance Tests
| Given | When | Then |
| --- | --- | --- |
| A provider command that fails and prints a reason (e.g. "Login canceled") | The command is invoked | The raised error message contains the printed reason |
| Devin is configured as the provider and `devin auth status` reports "Not logged in" | Doctor runs its config checks | The check fails and mentions "Devin CLI" |
| Devin is configured as the provider, `devin auth status` reports "Not logged in", and doctor runs inside a Devin Desktop session | Doctor runs its config checks | The check warns instead of failing |
| Devin is configured as the provider and `devin auth status` reports a logged-in account | Doctor runs its config checks | The check passes and mentions "Devin CLI" |
