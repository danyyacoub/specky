#!/bin/sh
# Report the state of this repo's specky setup: toolchain, config, git hook, index, site, and
# whether recent commits actually got documented. Run this before debugging "why didn't a doc get
# generated" instead of manually checking each piece.
#
# The checks themselves live in src/specky/doctor.py, so an installed specky has them too
# (`specky doctor`, `--json` for machine use). This script only exists so the entry point
# documented in AGENTS.md keeps working from a checkout.
set -eu

cd "$(cd "$(dirname "$0")/.." && pwd)"
exec uv run specky doctor "$@"
