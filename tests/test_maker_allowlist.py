"""Two independent conditions to enable maker. One to kill it.

`TRADING_MAKER_ENABLED` is the global master. `TRADING_MAKER_ENABLED_SERIES` is
the per-series allow-list. A series is maker ONLY if the master is on AND the
series is listed; anything else is taker, including a series with perfect
evidence that nobody added.

The allow-list is per series and not per model because LAX carries 59% of the
weather tape. A model-level switch would license Denver on Los Angeles's
liquidity — the same failure as validating weather inside Kalshi's "General"
bucket, one layer further down.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def maker(reload_config):
    def _load(**env):
        return reload_config("src.execution.allowlist", **env)

    return _load


class TestTheTwoConditions:
    def test_master_off_and_series_listed_is_taker(self, maker):
        mod = maker(
            TRADING_MAKER_ENABLED="false",
            TRADING_MAKER_ENABLED_SERIES="KXHIGHLAX",
        )

        assert mod.maker_allowed_for("KXHIGHLAX-26AUG19-T83") is False

    def test_master_on_and_series_absent_is_taker(self, maker):
        mod = maker(
            TRADING_MAKER_ENABLED="true",
            TRADING_MAKER_ENABLED_SERIES="KXHIGHLAX",
        )

        assert mod.maker_allowed_for("KXHIGHDEN-26AUG19-T92") is False

    def test_both_conditions_met_is_maker(self, maker):
        mod = maker(
            TRADING_MAKER_ENABLED="true",
            TRADING_MAKER_ENABLED_SERIES="KXHIGHLAX,KXHIGHCHI",
        )

        assert mod.maker_allowed_for("KXHIGHLAX-26AUG19-T83") is True
        assert mod.maker_allowed_for("KXHIGHCHI-26AUG19-T85") is True

    def test_an_empty_list_is_maker_nowhere(self, maker):
        mod = maker(
            TRADING_MAKER_ENABLED="true", TRADING_MAKER_ENABLED_SERIES="",
        )

        assert mod.enabled_series() == []
        assert mod.maker_allowed_for("KXHIGHLAX-26AUG19-T83") is False


class TestDefaults:
    def test_the_shipped_defaults_are_maker_nowhere(self, maker):
        """Doing nothing keeps every series on taker."""
        mod = maker()

        assert mod.maker_allowed_for("KXHIGHLAX-26AUG19-T83") is False

    def test_the_default_series_list_is_empty_and_must_stay_empty(self, maker):
        """The L31 interaction, pinned.

        `_env_str` treats an empty value as absent and returns the coded
        default. That is safe ONLY because this default is empty: absent,
        empty and "maker nowhere" all coincide. A non-empty default here could
        not be turned off by clearing the repository variable.
        """
        import src.trading_config as config

        importlib.reload(config)

        assert config.MAKER_ENABLED_SERIES == ""
        assert config.MAKER_ENABLED is False


class TestMatching:
    def test_matching_is_on_the_whole_series_token(self, maker):
        """`KXHIGH` must not enable all seven cities at once."""
        mod = maker(
            TRADING_MAKER_ENABLED="true", TRADING_MAKER_ENABLED_SERIES="KXHIGH",
        )

        assert mod.maker_allowed_for("KXHIGHNY-26AUG19-T90") is False

    def test_matching_is_case_insensitive(self, maker):
        mod = maker(
            TRADING_MAKER_ENABLED="true",
            TRADING_MAKER_ENABLED_SERIES="kxhighlax",
        )

        assert mod.maker_allowed_for("KXHIGHLAX-26AUG19-T83") is True

    def test_an_unknown_ticker_is_taker(self, maker):
        mod = maker(
            TRADING_MAKER_ENABLED="true",
            TRADING_MAKER_ENABLED_SERIES="KXHIGHLAX",
        )

        assert mod.maker_allowed_for("") is False
        assert mod.maker_allowed_for("NONSENSE") is False


class TestOrderTypeResolution:
    def test_resolved_order_type_follows_the_allowlist(self, maker):
        mod = maker(
            TRADING_MAKER_ENABLED="true",
            TRADING_MAKER_ENABLED_SERIES="KXHIGHLAX",
        )

        assert mod.order_type_for("KXHIGHLAX-26AUG19-T83") == "maker"
        assert mod.order_type_for("KXHIGHDEN-26AUG19-T92") == "taker"

    def test_default_config_resolves_taker_everywhere(self, maker):
        mod = maker()

        assert mod.order_type_for("KXHIGHLAX-26AUG19-T83") == "taker"
