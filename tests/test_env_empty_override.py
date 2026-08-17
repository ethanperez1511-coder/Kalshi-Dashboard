"""An unset repository variable must not erase a coded default.

GitHub renders `${{ vars.NOT_SET }}` as an empty string, and the environment
variable is then PRESENT and EMPTY — not absent. `os.environ.get(key, default)`
returns "" in that case, because the key exists.

Measured 2026-08-17: wiring `TRADING_EXCLUDED_SERIES: ${{ vars.… }}` into
trade.yml before the variable existed set it to "", which parsed to an empty
exclusion list, which disabled the parlay filter entirely. 27,256 market rows
were written in one day, post-purge, with the filter believed live. The census
prefix breakdown showed the excluded tokens verbatim — not variants — which is
what proves the filter was not running rather than not matching.

The trade.yml comment I wrote at the time said "an unset variable renders as an
empty string and _env_bool falls through to the coded default, so absence
changes nothing". True of `_env_bool`, whose membership test rejects "". False
of `_env_str`. One sentence, asserted for both, correct for one.

So absence and emptiness now mean the same thing for every reader.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def config(reload_config):
    def _load(**env):
        return reload_config(**env)

    return _load


class TestEmptyMeansAbsent:
    def test_an_empty_string_var_keeps_the_default(self, config):
        module = config(TRADING_EXCLUDED_SERIES="")

        assert module.EXCLUDED_SERIES
        assert "KXMVECROSSCATEGORY" in module.excluded_series_list()

    def test_whitespace_only_also_keeps_the_default(self, config):
        module = config(TRADING_EXCLUDED_SERIES="   ")

        assert "KXMVECROSSCATEGORY" in module.excluded_series_list()

    def test_a_real_value_still_overrides(self, config):
        module = config(TRADING_EXCLUDED_SERIES="KXSPAM,KXJUNK")

        assert module.excluded_series_list() == ["KXSPAM", "KXJUNK"]

    def test_the_ingested_weather_series_survive_an_empty_var(self, config):
        """Same trap, worse blast radius: an empty value here would stop
        ingesting every temperature series the system trades."""
        module = config(TRADING_INGEST_SERIES_TICKERS="")

        assert "KXHIGHNY" in module.ingest_series_list()
        assert len(module.ingest_series_list()) == 7


class TestTheOtherReadersWereAlreadySafe:
    def test_env_bool_rejects_empty(self, config):
        module = config()

        assert module._env_bool("DEFINITELY_UNSET", True) is True

    def test_env_int_and_float_reject_empty(self, config, monkeypatch):
        module = config()
        monkeypatch.setenv("TRADING_SOME_NUMBER", "")

        assert module._env_int("TRADING_SOME_NUMBER", 7) == 7
        assert module._env_float("TRADING_SOME_NUMBER", 1.5) == 1.5


class TestTheFilterActuallyRuns:
    def test_the_default_exclusion_set_is_never_empty(self, config):
        """The shipped default is the whole defence. An empty set means the
        parlay firehose is uncapped, which is a two-day outage."""
        import src.ingestion.exclusions as exclusions

        config()
        importlib.reload(exclusions)

        assert exclusions.EXCLUDED_SERIES
        assert exclusions.is_excluded_series("KXMVECROSSCATEGORY-26AUG18-A")
        assert exclusions.is_excluded_series("KXMVESPORTSMULTIGAMEEXTENDED-S1-A")
