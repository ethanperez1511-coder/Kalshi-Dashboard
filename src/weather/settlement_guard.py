"""Does this contract still settle where and how we think it does?

Kalshi repointed all seven temperature series from the NWS Climatological
Report to The Weather Company on 2026-08-14. A guard existed and it fired —
as a `@pytest.mark.live` test, producing a red scheduled job three days running
while the pricing path knew nothing about it. Weather did not refuse over the
change; it was dark for an unrelated reason (the MOS blackout). Had it not
been, this system would have priced seven cities against a settlement authority
nobody had checked.

A guard that runs only in CI gates CI. This one runs against the rules text
stored on the market row, on every scoring pass, and a contract whose
settlement description cannot be recognised produces no price.

Two independent checks, which fail differently on purpose:

  SITE       the CLI product code — CLINYC, CLIMDW, CLIMIA, CLIDEN, CLIAUS,
             CLILAX, CLIPHL. Vendor-independent, which is exactly why it
             survived the NWS -> TWC switch: it names the observing station,
             not the distributor. Matched as a token, so CLINYCX cannot
             satisfy CLINYC.

  AUTHORITY  the named settlement source, against the ones known to carry the
             CLI number. Measured 2026-08-14/15 across all seven cities, TWC's
             published values matched the NWS CLI products 14/14 — the switch
             changed the distributor, not the number. A name outside this list
             has not been checked against anything and is not something to
             price through.

The previous NWS wording still verifies. Refusing it would refuse a contract
that settles identically, and the guard exists to catch changes in what settles
the market, not changes in how it is phrased.
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

from src.weather.stations import station_for_market

logger = logging.getLogger(__name__)

# Distributors measured to publish the NWS CLI number for these stations. The
# NWS entries cover the pre-2026-08-14 wording, which settles identically.
ACCEPTED_AUTHORITIES = (
    "the weather company",
    "national weather service",
    "climatological report",
)


def verify_settlement(
    market_id: str, rules_text: Optional[str],
) -> Tuple[bool, str]:
    """(ok, reason). False means: do not price this contract."""
    station = station_for_market(market_id)
    if station is None:
        return False, "settlement_no_station_mapping"

    text = (rules_text or "").strip().lower()
    if not text:
        # Absent evidence is not evidence. A market row with no rules text
        # cannot demonstrate where it settles, and assuming it is unchanged is
        # the assumption this module exists to stop.
        return False, "settlement_rules_missing"

    # The site may be named either way: the CLI product code (current wording)
    # or the site in prose (pre-2026-08-14). Both identify the same station and
    # settle identically, so either satisfies this — the guard is here to catch
    # a change in WHAT settles the market, not a change in how it is phrased.
    #
    # Token match on the code: a longer code that merely starts with ours is a
    # DIFFERENT product for a different site, and substring matching passes it.
    marker = station.cli_marker.lower()
    by_code = re.search(rf"\b{re.escape(marker)}\b", text) is not None
    by_name = bool(station.rules_marker) and station.rules_marker.lower() in text
    if not (by_code or by_name):
        return False, "settlement_site_changed"

    if not any(name in text for name in ACCEPTED_AUTHORITIES):
        return False, "settlement_authority_unrecognised"

    return True, "verified"
