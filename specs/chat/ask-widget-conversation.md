---
type: feature
tags: [chat]
---

# Chat — Ask Widget Conversation

## What It Does

The Ask widget now remembers recent questions and answers within a browser tab, so follow-up questions like "why?" or "what about the other one?" can resolve against what was already asked. Conversations reset when you click **New**, and nothing is saved to disk — the history lives only in the server's memory while it's running.

## How It Works

1. **Browser generates a session ID** when the page loads — `crypto.randomUUID()` where it exists, otherwise a timestamp-plus-random string, since `randomUUID` needs a secure context that plain `http://` on a real hostname isn't — and stores it in the tab's sessionStorage.
2. **Each question includes the session ID** when sent to the server, along with its full text.
3. **Server stores recent turns** (questions and answers) for that session in memory, keyed by the session ID.
4. **History is folded into the prompt** above the retrieved context on follow-up questions, labeled so the model understands it as conversation context rather than a source to answer from.
5. **If a follow-up finds no matching docs**, the server re-queries using the previous question's words, which is what makes "why?" work at all.
6. **Server drops the conversation** when the reader clicks **New**, clears the browser storage, and sends a reset signal to drop it from memory too.

## Outcomes

| Scenario | Behavior |
|----------|----------|
| Reader asks "Why is this pattern used?" after a previous question | Server retrieves docs about the pattern and answers in context of that prior conversation, not in a vacuum |
| Reader navigates to another doc mid-conversation | Session ID persists in tab storage; conversation continues where it left off |
| Reader reloads the page | Transcript is preserved in sessionStorage; conversation resumes with the same session ID |
| Server restarts | All in-memory conversations are dropped; readers can click **New** and continue asking questions about the docs |
| Reader clicks **New** | Browser clears the session ID and transcript from storage; server removes that session from memory |

## Acceptance Tests

| Given | When | Then |
|-------|------|------|
| Reader has asked "What is module foo?" and server answered | Reader asks "Why?" in the same tab | Server retrieves context about foo and answers based on it, not treating "why" as a standalone question |
| Conversation exists for session ID "abc-123" | Server holds 6 turns for that session, then reader asks a 7th question | 7th turn is stored; oldest turn (the 1st) is dropped from memory |
| Reader has an open conversation in a tab | Reader clicks "New" | Browser clears the session ID and transcript; next question creates a new session and has no history |
| Session ID is 65 characters long or missing entirely | Reader sends a question | Server answers statelessly (as if no conversation ever existed) and does not store the turn |
| Server is running with 64 active conversations | A 65th unique session ID sends a question | Server drops the least recently used conversation and stores the new one |
