"""Market clock helpers. All times are US/Eastern."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def now_et(override: Optional[str] = None) -> datetime:
    """Current Eastern time. `override` / $BLITZ_NOW (ISO, read as Eastern) is for tests and replays."""
    raw = override or os.environ.get("BLITZ_NOW")
    if raw:
        parsed = datetime.fromisoformat(raw)
        return parsed.replace(tzinfo=ET) if parsed.tzinfo is None else parsed.astimezone(ET)
    return datetime.now(ET)


def is_trading_day(day: date, cfg: dict) -> bool:
    if day.weekday() >= 5:
        return False
    return day.isoformat() not in cfg.get("calendar", {}).get("holidays", [])


def add_trading_days(day: date, n: int, cfg: dict) -> date:
    out = day
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining > 0:
        out += timedelta(days=step)
        if is_trading_day(out, cfg):
            remaining -= 1
    return out


def trading_days_between(start: date, end: date, cfg: dict) -> int:
    """Trading days after `start` up to and including `end` (0 if end <= start)."""
    count = 0
    cur = start
    while cur < end:
        cur += timedelta(days=1)
        if is_trading_day(cur, cfg):
            count += 1
    return count


def _hm(text: str) -> Tuple[int, int]:
    h, m = text.split(":")
    return int(h), int(m)


def window_status(now: datetime, cfg: dict, entry: bool) -> Tuple[bool, str]:
    """Is `now` inside the window in which this kind of order may be sent?"""
    ex = cfg["execution"]
    if not is_trading_day(now.date(), cfg):
        return False, "market is closed today (weekend or holiday)"
    start, end = ex.get("entry_window_et" if entry else "trade_window_et", ["09:35", "15:58"])
    early = now.date().isoformat() in cfg.get("calendar", {}).get("early_closes", [])
    if early:
        end = ex.get("early_close_trade_end_et", "12:58")
        if entry:
            # On a half day the close run happens too late to enter; exits only.
            return False, "early-close day: no new entries"
    minutes = now.hour * 60 + now.minute
    sh, sm = _hm(start)
    eh, em = _hm(end)
    if minutes < sh * 60 + sm:
        return False, f"before the {start} ET window"
    if minutes >= eh * 60 + em:
        return False, f"after the {end} ET window"
    return True, "ok"
