#!/usr/bin/env node
// Render-time-only: turns one mermaid source string into an SVG string via
// beautiful-mermaid (zero DOM dependencies, so this runs in plain Node -- no
// jsdom/browser needed). Not shipped to readers; see ../../../../AGENTS.md.
//
// Protocol: read one JSON object from stdin `{ source, options }`, write the
// resulting SVG to stdout on success, or a message to stderr + exit(1) on failure.
// diagram_render.py treats a non-zero exit as "leave the doc's fenced source as-is".
import { renderMermaidSVGAsync } from "beautiful-mermaid";

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

const { source, options } = JSON.parse(await readStdin());
try {
  const svg = await renderMermaidSVGAsync(source, options);
  process.stdout.write(svg);
} catch (err) {
  process.stderr.write(String((err && err.message) || err));
  process.exit(1);
}
