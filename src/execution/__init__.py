"""Execution: how we trade, never whether.

Everything here sits downstream of an already-approved TradeDecision. No gate,
model, threshold or risk limit is reachable from this package except to
RE-check limits on a partial fill, which can only shrink an order.
"""
