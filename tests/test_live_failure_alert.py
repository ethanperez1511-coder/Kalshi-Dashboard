"""The alert that reports a failed guard must not be able to fail silently.

live-checks failed for three consecutive days in production (2026-08-14 to
08-16, all seven temperature series repointed at a new settlement source) and
no Telegram message arrived. The workflow step was configured correctly —
`if: failure()` present, secret names identical to trade.yml, which delivers.
The defect was one level down: `main()` called `alerter.send(...)`, discarded
the delivery bool that commit 7089853 added for exactly this purpose, and
returned 0 unconditionally. A refused send and a delivered send produced the
same green step and the same absent message.

That is the skipping-test bug in its purest form — something is watching, and
nothing is watching the watcher. These tests execute the entry point the way
Actions executes it (L27) and pin the one property that matters: an
undelivered alert never reports success.
"""
from __future__ import annotations

import runpy

import pytest


@pytest.fixture
def summary_path(monkeypatch, tmp_path):
    path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    monkeypatch.setenv("RUN_URL", "https://github.com/x/y/actions/runs/123")
    return path


class _Alerter:
    """Stand-in for src.alerts.Alerter with a scripted delivery outcome."""

    instances: list = []

    def __init__(self, delivered: bool):
        self._delivered = delivered
        self.sent: list = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return self._delivered

    @classmethod
    def factory(cls, delivered: bool):
        def _make(*args, **kwargs):
            instance = cls(delivered)
            cls.instances.append(instance)
            return instance

        cls.instances = []
        return _make


def _run_as_module(monkeypatch, delivered: bool) -> int:
    """Execute `python -m src.alert_live_failure` the way the workflow does.

    Patches the SOURCE module: runpy re-executes the file, so its
    `from src.alerts import Alerter` rebinds from src.alerts on every run and
    patching the entry module's attribute would be undone.
    """
    import src.alerts

    monkeypatch.setattr(src.alerts, "Alerter", _Alerter.factory(delivered))
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("src.alert_live_failure", run_name="__main__")
    return exc.value.code


class TestDeliveredAlert:
    def test_returns_zero_and_sends_one_message(self, monkeypatch, summary_path):
        code = _run_as_module(monkeypatch, delivered=True)

        assert code == 0
        assert len(_Alerter.instances) == 1
        sent = _Alerter.instances[0].sent
        assert len(sent) == 1
        assert "LIVE CHECKS FAILED" in sent[0]

    def test_run_url_reaches_the_message(self, monkeypatch, summary_path):
        _run_as_module(monkeypatch, delivered=True)

        assert "actions/runs/123" in _Alerter.instances[0].sent[0]

    def test_summary_records_the_delivery(self, monkeypatch, summary_path):
        _run_as_module(monkeypatch, delivered=True)

        assert "delivered" in summary_path.read_text().lower()


class TestUndeliveredAlert:
    """The whole point: non-delivery must be loud, and must never read green."""

    def test_refused_send_exits_non_zero(self, monkeypatch, summary_path):
        assert _run_as_module(monkeypatch, delivered=False) != 0

    def test_refused_send_says_so_in_the_run_summary(self, monkeypatch, summary_path):
        _run_as_module(monkeypatch, delivered=False)
        text = summary_path.read_text()

        assert "❌" in text
        # The operator reads this line instead of the missing Telegram message,
        # so it has to carry the run URL and say the alert did not go out.
        assert "did not" in text.lower() or "not delivered" in text.lower()
        assert "actions/runs/123" in text

    def test_delivered_and_refused_do_not_share_an_exit_code(
        self, monkeypatch, summary_path,
    ):
        """The regression, stated directly.

        Before the fix both paths returned 0 and the run summary was silent, so
        no observer — Telegram, the Actions UI, or a later audit — could tell
        an alerted failure from an unalerted one.
        """
        delivered = _run_as_module(monkeypatch, delivered=True)
        refused = _run_as_module(monkeypatch, delivered=False)

        assert delivered != refused


class TestMissingCredentials:
    """No token configured is the exact scenario that produced silence."""

    def test_real_alerter_without_credentials_exits_non_zero(
        self, monkeypatch, summary_path,
    ):
        monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

        # No patching: the real Alerter, disabled, must still be caught.
        with pytest.raises(SystemExit) as exc:
            runpy.run_module("src.alert_live_failure", run_name="__main__")

        assert exc.value.code != 0
        assert "did not" in summary_path.read_text().lower()

    def test_no_network_call_is_attempted_when_disabled(
        self, monkeypatch, summary_path,
    ):
        """A disabled alerter must fail on configuration, not on a timeout."""
        import src.alerts

        monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

        def _explode(*args, **kwargs):
            raise AssertionError("attempted an HTTP call with no credentials")

        monkeypatch.setattr(src.alerts.httpx, "post", _explode)

        with pytest.raises(SystemExit) as exc:
            runpy.run_module("src.alert_live_failure", run_name="__main__")

        assert exc.value.code != 0
