---
name: launch-viewer
description: Build and open the specky HTML doc viewer, with its Spec Assistant chat, for the current repo. Use when asked to launch, open, start, preview or browse the specky viewer, site or docs, or to run specky serve.
---

# Launch Viewer
Rebuild the doc site from `specs/` and serve it, so the user can browse it and ask the Spec
Assistant questions. In the Claude desktop app it opens in the browser pane; in a terminal it runs
as a background server.

## Steps

### 1. Build the site
`specky serve` only serves what `specky render-html` last wrote, and `render-html` reads the index,
so rebuild both. Neither calls an AI provider. Run as **one** Bash call, since shell functions don't
carry over between calls:

```bash
run_specky() { if command -v specky >/dev/null 2>&1; then specky "$@"; else uv run --project "${CLAUDE_PLUGIN_ROOT}" specky "$@"; fi; }
command -v specky || echo "specky not on PATH, using the plugin's copy through uv"
run_specky index && run_specky render-html
```

A function, not a `SPECKY="uv run …"` variable: zsh doesn't word-split an unquoted variable, so
`$SPECKY index` would look for a single command named `uv run --project …`. Remember which form
ran (the second line says); step 3 needs it.

- If it fails with "no documents indexed yet", the repo has no docs. Stop and suggest documenting a
  first feature (`specky document "<feature>"` or the `document-domain` skill).
- If neither `specky` nor `uv` is found, stop and point the user at the install steps in specky's
  README. Don't try to install it yourself.

### 2. Pick the port
Use `port` from the `[serve]` table in `specky.toml` at the repo root if it sets one, else `8420`
(the default `specky serve` would pick itself). Always pass it explicitly as `--port`, so the
server and anything waiting on that port agree.

If something is already listening there, check whether it's already the viewer: fetch
`http://127.0.0.1:<port>/` and look for `Spec Assistant` in the page. If it's there, the viewer is
already running and picks up the new build on reload, so skip to step 4. If it's something else,
pick a free port instead.

### 3. Start the server

**If you have a preview tool that starts servers from `.claude/launch.json`** (the Claude desktop
app's `preview_start`), use it. Don't start the server with Bash, because the pane only shows servers
it launched itself.

1. Read `.claude/launch.json` at the repo root. If it already has a configuration named
   `specky-serve`, keep it as is, unless it points at a path that no longer exists. A plugin update
   moves `${CLAUDE_PLUGIN_ROOT}`, so an entry written by an older version goes stale. Replace just
   that entry then.
2. Otherwise add this configuration, creating the file (`"version": "0.0.1"`,
   `"configurations": [...]`) if it doesn't exist and leaving every other configuration untouched:
   - with `specky` on PATH: `"runtimeExecutable": "specky"`,
     `"runtimeArgs": ["serve", "--port", "<port>"]`
   - otherwise: `"runtimeExecutable": "uv"`,
     `"runtimeArgs": ["run", "--project", "${CLAUDE_PLUGIN_ROOT}", "specky", "serve", "--port", "<port>"]`

   plus `"name": "specky-serve"` and `"port": <port>` (a number, not a string).
3. Call `preview_start` with `name: "specky-serve"`.

Open the viewer over `http://`, never a `file://` path to `.specky/site/`. The pane snapshots
`file://` pages without running their scripts, so search and the Spec Assistant would be dead.

**Otherwise, if you can run a background command** (Claude Code in a terminal): run
`specky serve --port <port>` in the background, or `uv run --project "${CLAUDE_PLUGIN_ROOT}" specky
serve --port <port>` if step 1 fell back to uv. Then open `http://127.0.0.1:<port>/` with `open`
(macOS) or `xdg-open` (Linux).

**Otherwise**, give the user the command to run and the URL to open, and stop there.

### 4. Confirm it's up
Wait for `specky serve: viewer on http://…` in the server output, or load the page and check it
isn't an error. If the server exited, show its last lines instead of retrying blindly.

## Report

- The URL, and how to stop it: `preview_stop` in the desktop app, killing the background process in
  a terminal.
- If you created or changed `.claude/launch.json`, say so. If the entry uses the `uv` form, mention
  that it contains this machine's plugin path, which the user may not want to commit.
- The Spec Assistant needs `[ai]` configured (`specky init`). Without it, the chat panel says it's
  offline and the rest of the viewer works normally. Don't send it a test question; that calls the
  user's provider.
- The site is a snapshot. After doc changes, run this skill again (or `specky index && specky
  render-html`) and reload the page.
