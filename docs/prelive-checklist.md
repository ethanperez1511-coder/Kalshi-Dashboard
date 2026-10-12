# Pre-live checklist

**Status as of 2026-10-12. Live mode is OFF and stays off.** The paper gate
has reached 50. Flipping `mode` is a human decision, and this list is what has
to be true first. An item is DONE only with the evidence named next to it.

| # | Item | Status | Evidence / what is missing |
|---|---|---|---|
| 1 | Decimal migration (fractional quantities) | **OPEN** | `trades.quantity` and `positions.quantity` are still `Integer` (src/models/trade.py:20, position.py:19). Kalshi prints fractional sizes (1541 of 2299 observed prints), and a live partial fill would be truncated, not rejected. `preflight.check_decimal_migration` reports it as a blocker. Needs Numeric columns and Decimal arithmetic on the money path; the migrator also needs an INTEGER→NUMERIC widening rule. |
| 2 | Fee-multiplier fix | **DONE** | Per-series schedule from `GET /series/{s}` (dca8942). Execution re-prices the traded side's after-fee EV at the true rate and refuses a series with no schedule we can vouch for (49f8cc0). The daily sweep covers all 19 traded series (ba3e6cb). Pinned: today's API fee equals the legacy formula at every price. Minor leftover: a dead `fee_rate=0.01` field passed to `EVResult` in run_trading.py; risk never reads it. |
| 3 | Forecast licensing | **OPEN (your attestation)** | The pricing path uses NWS MOS only (public domain), fetched from the Iowa Environmental Mesonet archive. Open-Meteo appears in comments, not in pricing. Open: IEM's terms for commercial use are unverified, and `TRADING_FORECAST_LICENSING_RESOLVED` is unset, so preflight blocks. Read IEM's terms, then set the attestation or don't. |
| 4 | Sports API access | **OPEN** | SportsOdds reaches no market today (the capped fetch is 100% excluded parlays; ruling B pending), and The Odds API free tier is 500 requests/month with commercial terms unverified. Recommendation: **scope the first live phase to weather only.** Sports then becomes "not applicable" by an explicit exclusion rather than a resolved licence. |
| 5 | Kill switches | **DONE** | 5c24fc6: seven latched switches; human clear by token only; trips demote the ramp. Running in every production cycle since 2026-10-09. |
| 6 | Size ramp | **BUILT, promotion blocked** | eed68e9: stage 0 = 1 contract; only lowers size; human promotion against evidence bars. It cannot be promoted until #7 exists, by design. |
| 7 | Ledger reconciler against Kalshi | **OPEN (new)** | Nothing compares our positions and settled PnL with Kalshi's `/portfolio` to the cent. The ramp's parity bar fails closed without it. Needed before the first promotion, and wanted from the first live trade. |
| 8 | Live-checks green on schedule | **OPEN (new)** | The scheduled live checks did not run on 10-10/11 (undeclared import, fixed in 5111daf). Both pass when run by hand today. Want 3 consecutive green scheduled runs on the fixed commit. |
| 9 | Where live runs | **OPEN (new)** | On GitHub Actions the cycle cadence is whatever the throttled scheduler delivers. Recommendation: no live flip before the Oracle VM has passed its 3 clean days and cut over. |
| 10 | Live order path exercised | **OPEN** | The live execution path is reviewed and tested but has never placed a real order. The ramp's stage 0 (1 contract) is how it first meets the exchange, after #1–#9. |
| 11 | Paper gate intact | **DONE** | `paper_trades_before_live == 50`; `preflight.check_live_gate_untouched`. |

**Ready to flip when:** 1, 3, 4, 7, 8 and 9 are closed. 10 is closed by the
flip itself, at 1 contract.
