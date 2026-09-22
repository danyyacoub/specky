"""What a user installs: the Claude Code plugin files and the package metadata behind them.

None of it is exercised by the rest of the suite, and every failure here is silent in production.
Claude Code only offers a plugin update when `plugin.json` changes its `version`, so a release that
bumps the package alone never reaches plugin users. A skill without frontmatter is never offered. A
hook script that lost its executable bit fails on every Bash call.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from specky import __version__

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JSON = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = ROOT / ".claude-plugin" / "marketplace.json"
SKILLS = sorted((ROOT / "skills").glob("*/SKILL.md"))


def test_plugin_version_matches_the_package():
    assert json.loads(PLUGIN_JSON.read_text())["version"] == __version__


def test_changelog_has_a_section_for_this_version():
    assert f"## [{__version__}]" in (ROOT / "CHANGELOG.md").read_text()


def test_the_marketplace_lists_this_plugin_from_this_repo():
    plugin = json.loads(PLUGIN_JSON.read_text())
    marketplace = json.loads(MARKETPLACE_JSON.read_text())

    [entry] = marketplace["plugins"]
    assert entry["name"] == plugin["name"]
    assert entry["source"] == "./"
    # One version, in plugin.json. A second copy here is one more thing a release can forget.
    assert "version" not in entry


def test_there_are_skills():
    assert {path.parent.name for path in SKILLS} >= {
        "setup",
        "document-domain",
        "explore-docs",
        "launch-viewer",
    }


@pytest.mark.parametrize("skill", SKILLS, ids=lambda path: path.parent.name)
def test_every_skill_has_a_name_and_description(skill: Path):
    text = skill.read_text()
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "no frontmatter"
    fields = dict(
        line.split(":", 1) for line in match.group(1).splitlines() if ":" in line
    )
    assert fields.get("name", "").strip() == skill.parent.name
    assert fields.get("description", "").strip()


def test_hook_commands_exist_and_are_executable():
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    commands = [
        hook["command"]
        for matchers in hooks.values()
        for matcher in matchers
        for hook in matcher["hooks"]
    ]

    assert commands
    for command in commands:
        path = ROOT / command.replace("${CLAUDE_PLUGIN_ROOT}/", "")
        assert path.is_file(), command
        assert os.access(path, os.X_OK), f"{command} is not executable"
