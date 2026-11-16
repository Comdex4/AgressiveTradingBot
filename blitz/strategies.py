"""Entry setups and exit rules. The same code drives the backtest and the live engine.

Three setups, all long-only, all judged on a daily bar (live: today's bar as it stands a few
minutes before the close):

  dip       Mean reversion. A stock in an uptrend that has been hit hard for a day or two
            (RSI(2) very low) tends to snap back within days. Exit on the snap-back.
  breakout  Momentum. A close at a new N-day high on heavy volume, closing near the day's high.
            Ride it while it keeps making higher lows.
  gap       Continuation of a big gap up that held its gains on heavy volume (news / earnings).

Each setup returns a Signal with its own stop. Exits are evaluated per position by exit_reason().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import indicators as ta
from .data import Series


class Ind:
    """Lazily computed, memoised indicators for one symbol."""

    def __init__(self, series: Series):
        self.s = series
        self._memo: Dict[tuple, list] = {}

    def _get(self, key: tuple, fn):
        if key not in self._memo:
            self._memo[key] = fn()
        return self._memo[key]

    def sma(self, n: int):
        return self._get(("sma", n), lambda: ta.sma(self.s.close, n))

    def rsi(self, n: int):
        return self._get(("rsi", n), lambda: ta.rsi(self.s.close, n))

    def atr(self, n: int):
        return self._get(("atr", n), lambda: ta.atr(self.s.high, self.s.low, self.s.close, n))

    def ibs(self):
        return self._get(("ibs",), lambda: ta.ibs(self.s.high, self.s.low, self.s.close))

    def prior_high(self, n: int):
        return self._get(("ph", n), lambda: ta.prior_max(self.s.high, n))

    def prior_close_high(self, n: int):
        return self._get(("pch", n), lambda: ta.prior_max(self.s.close, n))

    def avg_volume(self, n: int):
        return self._get(("av", n), lambda: ta.prior_mean(self.s.volume, n))

    def avg_dollar_volume(self, n: int):
        return self._get(("adv", n), lambda: ta.prior_mean(
            [c * v for c, v in zip(self.s.close, self.s.volume)], n))

    def roc(self, n: int):
        return self._get(("roc", n), lambda: ta.roc(self.s.close, n))

    def down_streak(self):
        return self._get(("ds",), lambda: ta.down_streak(self.s.close))


@dataclass
class Signal:
    symbol: str
    setup: str
    date: str
    price: float          # reference price (the bar's close)
    stop: float           # protective stop price
    score: float          # higher is better, comparable within a setup
    atr: float
    target: Optional[float] = None
    notes: Dict[str, float] = field(default_factory=dict)

    @property
    def stop_dist_pct(self) -> float:
        return max(0.0, (self.price - self.stop) / self.price)


# --------------------------------------------------------------------------- shared filters

def _liquid(ind: Ind, i: int, common: dict) -> bool:
    c = ind.s.close[i]
    if c < common.get("min_price", 5.0):
        return False
    adv = ind.avg_dollar_volume(20)[i]
    if adv is None or adv < common.get("min_avg_dollar_volume", 20_000_000):
        return False
    return True


def _stop(price: float, atr: float, mult: float, min_pct: float, max_pct: float) -> float:
    dist = min(max(atr * mult / price, min_pct), max_pct)
    return price * (1.0 - dist)


# --------------------------------------------------------------------------- setups

def dip_signal(ind: Ind, i: int, p: dict, common: dict) -> Optional[Signal]:
    s = ind.s
    trend = ind.sma(p.get("trend_sma", 200))[i]
    r = ind.rsi(p.get("rsi_period", 2))[i]
    a = ind.atr(14)[i]
    if trend is None or r is None or a is None or not _liquid(ind, i, common):
        return None
    c = s.close[i]
    if c <= trend or r > p.get("rsi_entry", 10):
        return None
    if ind.ibs()[i] > p.get("ibs_max", 1.0):
        return None
    if ind.down_streak()[i] < p.get("min_down_days", 0):
        return None
    min_atr_pct = p.get("min_atr_pct", 0.0)
    if a / c < min_atr_pct:
        return None
    # Do not catch a collapse: skip if today's drop is beyond max_day_drop_atr ATRs.
    if i > 0 and (s.close[i - 1] - c) > p.get("max_day_drop_atr", 99) * a:
        return None
    stop = _stop(c, a, p.get("stop_atr_mult", 2.5), p.get("stop_min_pct", 0.03), p.get("stop_max_pct", 0.12))
    rank = p.get("rank", "rsi")
    if rank == "atrpct":
        score = a / c
    elif rank == "momentum":
        score = ind.roc(126)[i] or 0.0
    elif rank == "drop":
        ref = s.close[i - 3] if i >= 3 else c
        score = (ref - c) / a
    else:
        score = 100.0 - r
    return Signal(s.symbol, "dip", s.dates[i], c, stop, score, a,
                  notes={"rsi": round(r, 2), "ibs": round(ind.ibs()[i], 3), "atr_pct": round(a / c, 4)})


def breakout_signal(ind: Ind, i: int, p: dict, common: dict) -> Optional[Signal]:
    s = ind.s
    n = p.get("lookback", 20)
    hi = ind.prior_high(n)[i]
    av = ind.avg_volume(20)[i]
    a = ind.atr(14)[i]
    fast = ind.sma(p.get("fast_sma", 50))[i]
    slow = ind.sma(p.get("slow_sma", 200))[i]
    if None in (hi, av, a, fast, slow) or not _liquid(ind, i, common):
        return None
    c = s.close[i]
    if c <= hi or not (c > fast > slow):
        return None
    rel_vol = s.volume[i] / av if av else 0.0
    if rel_vol < p.get("min_rel_volume", 1.5):
        return None
    if ind.ibs()[i] < p.get("min_ibs", 0.7):
        return None
    ext = ind.sma(20)[i]
    if ext and c > ext * (1.0 + p.get("max_extension_pct", 0.15)):
        return None
    if a / c < p.get("min_atr_pct", 0.0):
        return None
    stop = _stop(c, a, p.get("stop_atr_mult", 1.5), p.get("stop_min_pct", 0.02), p.get("stop_max_pct", 0.10))
    return Signal(s.symbol, "breakout", s.dates[i], c, stop, rel_vol, a,
                  notes={"rel_volume": round(rel_vol, 2), "ibs": round(ind.ibs()[i], 3)})


def gap_signal(ind: Ind, i: int, p: dict, common: dict) -> Optional[Signal]:
    s = ind.s
    if i < 1:
        return None
    av = ind.avg_volume(20)[i]
    a = ind.atr(14)[i]
    fast = ind.sma(p.get("fast_sma", 50))[i]
    if None in (av, a, fast) or not _liquid(ind, i, common):
        return None
    prev_close = s.close[i - 1]
    o, c = s.open[i], s.close[i]
    gap = o / prev_close - 1.0
    if gap < p.get("min_gap_pct", 0.04) or c < o or c <= fast:
        return None
    rel_vol = s.volume[i] / av if av else 0.0
    if rel_vol < p.get("min_rel_volume", 2.0):
        return None
    if ind.ibs()[i] < p.get("min_ibs", 0.6):
        return None
    floor = s.low[i]
    stop = max(floor * 0.995, c * (1.0 - p.get("stop_max_pct", 0.10)))
    stop = min(stop, c * (1.0 - p.get("stop_min_pct", 0.02)))
    return Signal(s.symbol, "gap", s.dates[i], c, stop, gap * rel_vol, a,
                  notes={"gap_pct": round(gap, 4), "rel_volume": round(rel_vol, 2)})


SETUPS = {"dip": dip_signal, "breakout": breakout_signal, "gap": gap_signal}


def find_signals(ind: Ind, i: int, setups_cfg: dict, common: dict, enabled: Optional[List[str]] = None) -> List[Signal]:
    out: List[Signal] = []
    for name, fn in SETUPS.items():
        cfg = setups_cfg.get(name, {})
        if not cfg.get("enabled", False):
            continue
        if enabled is not None and name not in enabled:
            continue
        sig = fn(ind, i, cfg, common)
        if sig is not None:
            out.append(sig)
    return out


# --------------------------------------------------------------------------- exits

def exit_reason(setup: str, ind: Ind, i: int, entry_price: float, bars_held: int, p: dict) -> Optional[str]:
    """Close-based exit for an open position, judged on bar i. None means keep holding.

    bars_held counts trading days since entry (0 on the entry day).
    Intraday stops and targets are handled by the caller, which sees live prices.
    """
    s = ind.s
    c = s.close[i]
    if bars_held <= 0:
        return None
    if setup == "dip":
        exit_sma = ind.sma(p.get("exit_sma", 5))[i]
        if exit_sma is not None and c > exit_sma:
            return "snapback_above_sma"
        r = ind.rsi(p.get("rsi_period", 2))[i]
        if r is not None and r >= p.get("rsi_exit", 101):
            return "rsi_recovered"
        if p.get("exit_above_prior_high", False) and i > 0 and c > s.high[i - 1]:
            return "close_above_prior_high"
    elif setup in ("breakout", "gap"):
        if i > 0 and c < s.low[i - 1]:
            return "close_below_prior_low"
    max_hold = p.get("max_hold_days", 3)
    if bars_held >= max_hold:
        # Let a clear winner run a little: still in profit and closing strong.
        extend = p.get("extend_hold_days", 0)
        if extend and bars_held < max_hold + extend and c > entry_price and ind.ibs()[i] >= 0.5 \
                and setup != "dip":
            return None
        return "time_stop"
    return None


def momentum_ranks(inds: Dict[str, Ind], date: str, lookback: int = 126) -> Dict[str, float]:
    """Cross-sectional percentile (0 worst .. 1 best) of each symbol's `lookback`-day return."""
    vals = []
    for sym, ind in inds.items():
        i = ind.s.index_of(date)
        if i is None:
            continue
        r = ind.roc(lookback)[i]
        if r is not None:
            vals.append((r, sym))
    vals.sort()
    n = len(vals)
    return {sym: (k / (n - 1) if n > 1 else 1.0) for k, (_, sym) in enumerate(vals)}
