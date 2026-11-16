"""Position sizing, shared by the backtest and the live engine."""

from __future__ import annotations


def position_dollars(equity: float, stop_dist_pct: float, sizing: dict, size_mult: float = 1.0) -> float:
    """Dollars to put into one new position.

    Risk-based: lose at most `risk_per_trade_pct` of sleeve equity if the stop is hit, capped at
    `max_position_pct` of sleeve equity. `size_mult` scales it down (bear regime, drawdown).
    """
    cap = equity * sizing.get("max_position_pct", 0.34)
    risk_pct = sizing.get("risk_per_trade_pct", 0.0)
    if risk_pct and stop_dist_pct > 0:
        by_risk = equity * risk_pct / stop_dist_pct
        dollars = min(cap, by_risk)
    else:
        dollars = cap
    return max(0.0, dollars * size_mult)


def disaster_stop_price(fill: float, stop_dist_pct: float, checks: dict) -> float:
    """The wider emergency stop that is live between close runs.

    The normal stop is only judged at the close (intraday stops whipsaw this kind of trade), so
    this one exists to cap the damage from a genuine collapse during the day.
    """
    mult = checks.get("disaster_stop_mult", 1.6)
    dist = min(stop_dist_pct * mult, checks.get("disaster_stop_max_pct", 0.22))
    return fill * (1.0 - dist)
