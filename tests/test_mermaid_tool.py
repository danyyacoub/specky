"""Finding and installing the Node mermaid renderer.

The failure this guards against is invisible by design: an installed specky ships `render.mjs`
without its `node_modules`, `render_mermaid_svg` returns None, and every diagram in every doc
silently stays fenced text. So the resolution order and `setup()` are tested directly rather than
through a render.

`npm` is faked with a shell script on PATH — the real one would need the network, and what's under
test here is specky's side of the contract, not npm's.
"""

import os
import stat
from pathlib import Path

import pytest

from specky import mermaid_tool


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Never touch the developer's real ~/.cache/specky, and never let its state decide a test."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv(mermaid_tool.ENV_VAR, raising=False)
    return tmp_path


def _install_marker(path: Path) -> None:
    """The two things `is_installed` looks for."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "render.mjs").write_text("// stub\n")
    (path / "node_modules").mkdir(exist_ok=True)


def _fake_npm(tmp_path: Path, monkeypatch, exit_code: int = 0, creates_modules: bool = True) -> Path:
    """A stand-in `npm` on PATH that records its argv and (optionally) creates node_modules."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = bin_dir / "npm.log"
    script = bin_dir / "npm"
    # `npm install --prefix <dir>` — so the prefix is $3. Real npm also rewrites the lockfile in
    # place while installing, which is exactly what used to make every re-run reinstall.
    mkdir_line = (
        'mkdir -p "$3/node_modules"\n'
        '[ -f "$3/package-lock.json" ] && printf \'\\n\' >> "$3/package-lock.json"\n'
        if creates_modules
        else "true\n"
    )
    script.write_text(
        f'#!/bin/sh\necho "$@" >> "{log}"\n{mkdir_line}'
        f'{"echo npm-said-no >&2" if exit_code else "true"}\nexit {exit_code}\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


# --- which copy of the tool gets used ----------------------------------------------------


def test_the_env_override_wins_over_everything(tmp_path, monkeypatch):
    override = tmp_path / "packaged"
    _install_marker(override)
    _install_marker(mermaid_tool.cache_dir())
    monkeypatch.setenv(mermaid_tool.ENV_VAR, str(override))

    assert mermaid_tool.tool_dir() == override
    assert mermaid_tool.candidate_dirs()[0] == override


def test_the_cache_dir_is_preferred_over_the_in_package_copy(tmp_path):
    """The in-package copy is wiped by the next `pip install --upgrade specky`, so an install that
    survives upgrades has to win — otherwise `setup-diagrams` would be undone by every upgrade."""
    _install_marker(mermaid_tool.cache_dir())
    assert mermaid_tool.tool_dir() == mermaid_tool.cache_dir()
    assert mermaid_tool.candidate_dirs()[-1] == mermaid_tool.SOURCE_DIR


def test_a_directory_with_the_script_but_no_dependencies_is_skipped(tmp_path, monkeypatch):
    """Exactly what an installed specky's package directory looks like: render.mjs shipped in the
    wheel, node_modules gitignored and therefore never built."""
    half = tmp_path / "half"
    half.mkdir()
    (half / "render.mjs").write_text("// stub\n")
    monkeypatch.setenv(mermaid_tool.ENV_VAR, str(half))
    assert not mermaid_tool.is_installed(half)

    _install_marker(mermaid_tool.cache_dir())
    assert mermaid_tool.tool_dir() == mermaid_tool.cache_dir()


def test_nothing_installed_anywhere_is_none_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv(mermaid_tool.ENV_VAR, str(tmp_path / "nope"))
    monkeypatch.setattr(mermaid_tool, "SOURCE_DIR", tmp_path / "also-nope")
    assert mermaid_tool.tool_dir() is None


def test_xdg_cache_home_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert mermaid_tool.cache_dir() == tmp_path / "xdg" / "specky" / "mermaid-render"


# --- installing it -----------------------------------------------------------------------


def test_setup_copies_the_tool_and_installs_its_dependencies(tmp_path, monkeypatch):
    log = _fake_npm(tmp_path, monkeypatch)
    lines = mermaid_tool.setup()

    target = mermaid_tool.cache_dir()
    assert (target / "render.mjs").read_bytes() == (mermaid_tool.SOURCE_DIR / "render.mjs").read_bytes()
    assert (target / "package.json").is_file()
    assert f"install --prefix {target}" in log.read_text()
    assert mermaid_tool.tool_dir() == target
    assert any("installed Node dependencies" in line for line in lines)


def test_a_second_run_does_nothing_and_says_so(tmp_path, monkeypatch):
    """Including the lockfile npm just rewrote under it — that rewrite used to read as a change and
    reinstall the whole tree on every single run."""
    log = _fake_npm(tmp_path, monkeypatch)
    mermaid_tool.setup()
    calls = len(log.read_text().splitlines())

    lines = mermaid_tool.setup()
    assert lines[-1].endswith("nothing else to do")
    assert len(log.read_text().splitlines()) == calls  # npm not called again


def test_a_changed_package_json_triggers_a_reinstall(tmp_path, monkeypatch):
    """Compared by contents, not mtime: a specky upgrade rewrites package.json either way, and
    only a real dependency change should cost the user an npm install."""
    log = _fake_npm(tmp_path, monkeypatch)
    mermaid_tool.setup()
    (mermaid_tool.cache_dir() / "package.json").write_text('{"name": "stale"}\n')

    mermaid_tool.setup()
    assert len(log.read_text().splitlines()) == 2


def test_a_new_render_script_is_copied_without_a_reinstall(tmp_path, monkeypatch):
    """render.mjs is just a script — replacing it costs a copy, not a dependency install."""
    log = _fake_npm(tmp_path, monkeypatch)
    mermaid_tool.setup()
    (mermaid_tool.cache_dir() / "render.mjs").write_text("// stale\n")

    lines = mermaid_tool.setup()
    assert (mermaid_tool.cache_dir() / "render.mjs").read_bytes() == (
        mermaid_tool.SOURCE_DIR / "render.mjs"
    ).read_bytes()
    assert any("updated render.mjs" in line for line in lines)
    assert len(log.read_text().splitlines()) == 1  # npm not called again


def test_force_reinstalls_an_apparently_healthy_copy(tmp_path, monkeypatch):
    log = _fake_npm(tmp_path, monkeypatch)
    mermaid_tool.setup()
    mermaid_tool.setup(force=True)
    assert len(log.read_text().splitlines()) == 2


def test_a_missing_npm_is_a_real_error_not_a_silent_skip(tmp_path, monkeypatch):
    """`render-html` degrades quietly on purpose; a command the user typed must not."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(RuntimeError, match="could not run npm"):
        mermaid_tool.setup()


def test_a_failing_npm_reports_its_last_line(tmp_path, monkeypatch):
    _fake_npm(tmp_path, monkeypatch, exit_code=1, creates_modules=False)
    with pytest.raises(RuntimeError, match="npm install failed: npm-said-no"):
        mermaid_tool.setup()


def test_a_lying_npm_is_caught(tmp_path, monkeypatch):
    _fake_npm(tmp_path, monkeypatch, exit_code=0, creates_modules=False)
    with pytest.raises(RuntimeError, match="node_modules is missing"):
        mermaid_tool.setup()
