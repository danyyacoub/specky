"""Where the Node mermaid renderer lives, and how to install it.

`specky render-html` shells out to a small Node tool (`vendor/mermaid-render/`) to turn each
```mermaid``` fence into a static SVG. That tool's `node_modules/` is a build artifact: gitignored
in this repo, and therefore absent from the built wheel, so an *installed* specky has `render.mjs`
and `package.json` and none of their dependencies. Installing them into the package directory
wouldn't fix it either — that directory may not be writable, and pip removes the old tree on
upgrade, taking them with it. Since a missing renderer degrades silently by design (the fence is
left as readable text), nobody would ever report the resulting "diagrams never work" either.

So the tool directory is resolved at runtime, first usable candidate winning:

1. `$SPECKY_MERMAID_DIR`, for anyone packaging specky themselves.
2. `~/.cache/specky/mermaid-render` (honouring `$XDG_CACHE_HOME`) — what `specky setup-diagrams`
   populates, and the one location that survives a specky upgrade.
3. The in-package `vendor/mermaid-render`, which is where a source checkout's own `npm install`
   lands, so contributors need no extra step.

"Usable" means both `render.mjs` and `node_modules/` are present; a candidate with the script but
no dependencies is skipped rather than tried and failed.

This module deliberately imports nothing but the standard library — `specky doctor` asks it about
diagram support, and shouldn't pay for markdown/jinja to find out.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SOURCE_DIR = Path(__file__).parent / "vendor" / "mermaid-render"

# Copied into the cache dir by `setup()`. package-lock.json is optional — it pins the install when
# it ships, and its absence just means npm resolves the range itself.
TOOL_FILES = ("render.mjs", "package.json", "package-lock.json")

# The one file whose contents decide whether `npm install` has to run again. Not the lockfile:
# npm rewrites it in place during install (filling in what it actually resolved), so comparing the
# installed copy against the packaged one reports a difference after every install, forever.
# Not render.mjs either — a new script needs copying, not reinstalling.
DEPENDENCY_MANIFEST = "package.json"

ENV_VAR = "SPECKY_MERMAID_DIR"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "specky" / "mermaid-render"


def candidate_dirs() -> list[Path]:
    """Every directory that could hold the tool, in resolution order."""
    override = os.environ.get(ENV_VAR)
    dirs = [Path(override)] if override else []
    return [*dirs, cache_dir(), SOURCE_DIR]


def is_installed(path: Path) -> bool:
    return (path / "render.mjs").is_file() and (path / "node_modules").is_dir()


def tool_dir() -> Path | None:
    """The directory to run `node render.mjs` in, or None if the tool isn't installed anywhere.

    None is not an error: `render_mermaid_svg` treats it the same as Node being absent and leaves
    the fenced source as text. `specky setup-diagrams` is what turns None into a path.
    """
    for path in candidate_dirs():
        if is_installed(path):
            return path
    return None


def _copy_if_changed(source: Path, target: Path) -> bool:
    """True if `target` was written. Compares contents, not mtimes: a specky upgrade rewrites
    `package.json` with a fresh mtime whether or not the dependency ranges moved, and the point of
    this check is to answer "does npm need to run again".
    """
    if not source.is_file():
        return False  # package-lock.json in a checkout that never ran npm install
    if target.is_file() and target.read_bytes() == source.read_bytes():
        return False
    shutil.copyfile(source, target)
    return True


def setup(target: Path | None = None, force: bool = False) -> list[str]:
    """Install the mermaid tool into `target` (the cache dir by default), and report what it did.

    Unlike `render_mermaid_svg`, this raises on failure. It's a command the user ran on purpose, so
    "npm isn't on your PATH" has to be said out loud rather than degraded past.
    """
    target = target or cache_dir()
    target.mkdir(parents=True, exist_ok=True)
    changed = [name for name in TOOL_FILES if _copy_if_changed(SOURCE_DIR / name, target / name)]

    if changed:
        lines = [f"specky setup-diagrams: updated {', '.join(changed)} in {target}"]
    else:
        lines = [f"specky setup-diagrams: {target} is already current"]

    if not force and DEPENDENCY_MANIFEST not in changed and is_installed(target):
        lines.append("specky setup-diagrams: dependencies already installed, nothing else to do")
        return lines

    try:
        proc = subprocess.run(
            ["npm", "install", "--prefix", str(target)], capture_output=True, text=True
        )
    except OSError as exc:
        raise RuntimeError(
            f"could not run npm ({exc}). Diagram rendering needs Node — install it, then re-run"
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise RuntimeError("npm install failed: " + (detail[-1] if detail else "no output"))
    if not is_installed(target):
        raise RuntimeError(f"npm install reported success but {target}/node_modules is missing")

    lines.append(f"specky setup-diagrams: installed Node dependencies in {target}")
    if shutil.which("node") is None:
        lines.append(
            "specky setup-diagrams: warning — `node` isn't on PATH, so diagrams still won't "
            "render until it is"
        )
    lines.append("specky setup-diagrams: re-run `specky render-html` to draw the diagrams")
    return lines
