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
COMMANDS = sorted((ROOT / "commands").glob("*.md"))


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
        "find-feature",
        "document-domain",
        "document-commits",
        "explore-docs",
        "launch-viewer",
    }


def _frontmatter(skill: Path) -> dict[str, str]:
    match = re.match(r"---\n(.*?)\n---\n", skill.read_text(), re.DOTALL)
    assert match, "no frontmatter"
    return dict(
        line.split(":", 1) for line in match.group(1).splitlines() if ":" in line
    )


@pytest.mark.parametrize("skill", SKILLS, ids=lambda path: path.parent.name)
def test_every_skill_has_a_name_and_description(skill: Path):
    fields = _frontmatter(skill)
    assert fields.get("name", "").strip() == skill.parent.name
    assert fields.get("description", "").strip()


@pytest.mark.parametrize("skill", SKILLS, ids=lambda path: path.parent.name)
def test_no_skill_pins_its_own_model(skill: Path):
    # Frontmatter is read before specky runs, so a model there is one nobody can change without
    # forking the plugin. The choice lives in `[skills] model` in the user's specky.toml instead.
    assert "model" not in _frontmatter(skill)


def test_the_doc_skills_read_their_model_from_specky_toml():
    # setup asks and writes it; launch-viewer starts a server that has to outlive any subagent, so
    # it stays on the session's model.
    for name in ("find-feature", "explore-docs", "document-domain", "document-commits", "setup"):
        assert "`[skills]`" in (ROOT / "skills" / name / "SKILL.md").read_text(), name


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


# --- slash commands ----------------------------------------------------------------------------
# `commands/*.md` become `/specky:<name>` in every repo with the plugin, beside the skills — which
# share the namespace, so a command named like a skill would hide one of the two.


def test_there_are_commands_for_the_cli_workflows():
    assert {path.stem for path in COMMANDS} >= {"doctor", "check", "lint", "verify-migration", "search"}


@pytest.mark.parametrize("command", COMMANDS, ids=lambda path: path.stem)
def test_every_command_has_a_description(command: Path):
    assert _frontmatter(command).get("description", "").strip()


def test_no_command_shadows_a_skill():
    assert not {path.stem for path in COMMANDS} & {path.parent.name for path in SKILLS}


def _subcommands() -> set[str]:
    from specky.cli import build_parser

    parser = build_parser()
    (action,) = [a for a in parser._actions if a.dest == "command"]
    return set(action.choices)


@pytest.mark.parametrize("command", COMMANDS, ids=lambda path: path.stem)
def test_every_specky_subcommand_a_command_runs_exists(command: Path):
    """A renamed subcommand would leave a command telling the agent to run something that errors,
    in every repo with the plugin. Checked in code spans and fenced lines, where commands are run."""
    text = command.read_text()
    code = "\n".join(re.findall(r"`([^`\n]+)`", text) + re.findall(r"```bash\n(.*?)```", text, re.S))
    named = set(re.findall(r"(?<![\w-])specky ([a-z][a-z-]+)", code))
    assert named, "a command that runs no specky subcommand"
    assert named <= _subcommands(), named - _subcommands()


@pytest.mark.parametrize("command", COMMANDS, ids=lambda path: path.stem)
def test_arguments_are_never_shell_expanded(command: Path):
    """`$ARGUMENTS` is replaced as text before the agent reads the command, so `${ARGUMENTS:+…}`
    arrives as `${whatever-the-user-typed:+…}` — a bash syntax error, or worse."""
    assert "${ARGUMENTS" not in command.read_text()
