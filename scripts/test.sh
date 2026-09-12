#!/bin/sh
# Unit + integration tests (tests/). Every test runs against a throwaway git repo in a
# tmpdir, so this never touches this repo's specs/ or .specky/ — unlike smoke-test.sh,
# which exercises the real CLI against the real tree. Run both before committing an
# engine change; this one first, it's the faster failure.
#
# Any extra arguments are passed straight through to pytest:
#   scripts/test.sh tests/test_db.py -k fts -x
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> pytest"
exec uv run pytest "$@"
