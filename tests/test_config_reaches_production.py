"""A config flag the workflow does not pass is not configurable.

`TRADING_SHADOW_MAKER_ENABLED` and `TRADING_EXCLUDED_SERIES` are read by
`trading_config` from the environment, and trade.yml passed neither. Setting
the repository variable would have changed nothing: the pipeline would read the
default, no shadow rows would appear, and the operator would be left looking
for a bug in the simulator.

The same shape as L28 one layer down. The value existed, the reader existed,
and the path between them did not — so "configurable" was a property of the
code and not of the deployment.

This asserts the path, for every flag the operator is expected to set without a
deploy.
"""
from __future__ import annotations

import pathlib

import pytest

WORKFLOW = pathlib.Path(".github/workflows/trade.yml")

# Flags documented as operator-settable. Adding one here without wiring it into
# trade.yml fails this test, which is the point.
OPERATOR_SETTABLE = [
    "TRADING_SHADOW_MAKER_ENABLED",
    "TRADING_EXCLUDED_SERIES",
]


@pytest.fixture(scope="module")
def workflow_text():
    assert WORKFLOW.exists(), f"{WORKFLOW} is missing"
    return WORKFLOW.read_text()


@pytest.mark.parametrize("flag", OPERATOR_SETTABLE)
def test_the_cycle_job_passes_the_flag(workflow_text, flag):
    assert f"{flag}:" in workflow_text, (
        f"{flag} is read by trading_config but trade.yml never passes it. "
        f"Setting the repository variable would silently do nothing."
    )


@pytest.mark.parametrize("flag", OPERATOR_SETTABLE)
def test_the_flag_is_sourced_from_a_repository_variable(workflow_text, flag):
    """`vars`, not `secrets`: these are configuration, not credentials, and a
    value nobody can read back is a value nobody can verify was set."""
    assert f"${{{{ vars.{flag} }}}}" in workflow_text, (
        f"{flag} must be wired to vars.{flag} so it is auditable in the UI"
    )


def test_shadow_maker_still_defaults_to_false_in_code():
    """The workflow passing an unset variable must not enable anything.

    An unset repository variable renders as an empty string, and _env_bool
    falls through to the default for anything it does not recognise.
    """
    from src.trading_config import _env_bool

    assert _env_bool("TRADING_SHADOW_MAKER_ENABLED", False) is False
    assert _env_bool("DEFINITELY_UNSET_FLAG_NAME", False) is False
