"""Shared fixtures: synthetic price series and an isolated state directory."""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta

from blitz.clock import ET
from blitz.config import load_config
from blitz.data import Series

TODAY = date(2026, 11, 16)  # a Monday


def at(hhmm: str = "15:40", day: date = TODAY) -> datetime:
    h, m = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=ET)


def weekdays_ending(end: date, n: int) -> list:
    out = []
    cur = end
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur -= timedelta(days=1)
    return out[::-1]


def make_series(symbol: str, n: int = 320, start: float = 50.0, drift: float = 0.002,
                day_range: float = 0.03, tail=(), volume: float = 5_000_000, end: date = TODAY) -> Series:
    """A steady uptrend with a fixed daily range; `tail` overrides the last daily returns."""
    dates = weekdays_ending(end, n)
    s = Series(symbol)
    price = start
    for k, d in enumerate(dates):
        back = n - 1 - k
        ret = tail[len(tail) - 1 - back] if back < len(tail) else drift
        prev = price
        price = prev * (1.0 + ret)
        hi = max(prev, price) * (1.0 + day_range / 2.0)
        lo = min(prev, price) * (1.0 - day_range / 2.0)
        s.append(d, prev, hi, lo, price, volume)
    return s


def dip_series(symbol: str = "DIPP", **kw) -> Series:
    """Uptrend, then two hard down days: RSI(2) collapses while price stays above the 200-day."""
    kw.setdefault("drift", 0.004)  # the strongest name in the test universe, so it ranks as a leader
    return make_series(symbol, tail=(-0.04, -0.05), **kw)


def breakout_series(symbol: str = "BRKO") -> Series:
    s = make_series(symbol, drift=0.001, tail=(0.06,))
    s.volume[-1] = s.volume[-2] * 3.0
    s.high[-1] = s.close[-1] * 1.002
    return s


class StateCase(unittest.TestCase):
    """Each test gets its own empty state directory and a small-universe config."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="blitz-test-")
        self._old = os.environ.get("BLITZ_STATE_DIR")
        os.environ["BLITZ_STATE_DIR"] = self.tmp
        self.cfg = copy.deepcopy(load_config())
        self.cfg["universe"]["symbols"] = ["DIPP", "BRKO", "CALM", "SPY"]

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("BLITZ_STATE_DIR", None)
        else:
            os.environ["BLITZ_STATE_DIR"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def market(self) -> dict:
        return {
            "SPY": make_series("SPY", start=400.0, day_range=0.01),
            "CALM": make_series("CALM", day_range=0.03),
            "DIPP": dip_series(),
            "BRKO": breakout_series(),
        }
