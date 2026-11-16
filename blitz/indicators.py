"""Technical indicators over plain lists of floats.

Every function returns a list the same length as its input, with None where the
indicator has not warmed up yet. Index i only ever uses data up to and including i.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

Num = Optional[float]


def sma(xs: Sequence[float], n: int) -> List[Num]:
    out: List[Num] = [None] * len(xs)
    if n <= 0:
        return out
    total = 0.0
    for i, x in enumerate(xs):
        total += x
        if i >= n:
            total -= xs[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def rsi(closes: Sequence[float], n: int) -> List[Num]:
    """Wilder's RSI."""
    out: List[Num] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gain = 0.0
    loss = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gain += d
        else:
            loss -= d
    avg_gain = gain / n
    avg_loss = loss / n
    out[n] = _rsi_value(avg_gain, avg_loss)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (n - 1) + (d if d > 0 else 0.0)) / n
        avg_loss = (avg_loss * (n - 1) + (-d if d < 0 else 0.0)) / n
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> List[float]:
    out: List[float] = []
    for i in range(len(closes)):
        if i == 0:
            out.append(highs[i] - lows[i])
        else:
            pc = closes[i - 1]
            out.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int) -> List[Num]:
    """Wilder's average true range."""
    tr = true_range(highs, lows, closes)
    out: List[Num] = [None] * len(closes)
    if len(closes) < n:
        return out
    value = sum(tr[:n]) / n
    out[n - 1] = value
    for i in range(n, len(closes)):
        value = (value * (n - 1) + tr[i]) / n
        out[i] = value
    return out


def ibs(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> List[float]:
    """Internal bar strength: where the close sits in the day's range, 0 (low) to 1 (high)."""
    out: List[float] = []
    for h, l, c in zip(highs, lows, closes):
        out.append(0.5 if h <= l else (c - l) / (h - l))
    return out


def prior_max(xs: Sequence[float], n: int) -> List[Num]:
    """Highest value of the n bars BEFORE index i (i itself excluded)."""
    out: List[Num] = [None] * len(xs)
    for i in range(n, len(xs)):
        out[i] = max(xs[i - n:i])
    return out


def prior_min(xs: Sequence[float], n: int) -> List[Num]:
    out: List[Num] = [None] * len(xs)
    for i in range(n, len(xs)):
        out[i] = min(xs[i - n:i])
    return out


def prior_mean(xs: Sequence[float], n: int) -> List[Num]:
    """Mean of the n bars BEFORE index i (i itself excluded)."""
    out: List[Num] = [None] * len(xs)
    total = 0.0
    for i, x in enumerate(xs):
        if i >= n:
            out[i] = total / n
            total -= xs[i - n]
        total += x
    return out


def roc(xs: Sequence[float], n: int) -> List[Num]:
    """Rate of change over n bars, as a fraction."""
    out: List[Num] = [None] * len(xs)
    for i in range(n, len(xs)):
        base = xs[i - n]
        out[i] = (xs[i] / base - 1.0) if base else None
    return out


def down_streak(closes: Sequence[float]) -> List[int]:
    """Number of consecutive lower closes ending at index i."""
    out: List[int] = []
    run = 0
    for i, c in enumerate(closes):
        if i > 0 and c < closes[i - 1]:
            run += 1
        else:
            run = 0
        out.append(run)
    return out
