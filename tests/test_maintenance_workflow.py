"""maintenance.yml's action list and its dispatch script must agree (L27).

An option with no case branch exits 2 at dispatch time; a branch with no
option is unreachable. Both are invisible until the moment an action is needed.
"""
import pathlib
import re

import yaml

WF = pathlib.Path(".github/workflows/maintenance.yml")


def _doc():
    return yaml.safe_load(WF.read_text())


def test_every_action_has_a_branch_and_every_branch_an_action():
    doc = _doc()
    options = set(doc[True]["workflow_dispatch"]["inputs"]["action"]["options"])
    script = doc["jobs"]["run"]["steps"][-1]["run"]
    branches = set(re.findall(r"^\s+([a-z0-9-]+)\)", script, re.M))
    assert options == branches


def test_the_default_action_is_read_only():
    assert _doc()[True]["workflow_dispatch"]["inputs"]["action"]["default"] == "db-stats"


def test_inputs_reach_the_shell_through_the_environment_only():
    """`${{ inputs.x }}` inside run: is a shell injection."""
    script = _doc()["jobs"]["run"]["steps"][-1]["run"]
    assert "${{" not in script
