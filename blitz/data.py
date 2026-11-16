"""Daily price bars: fetch, cache, and load.

Two sources:
  * Yahoo's public chart endpoint, fetched by this module. It keeps the heavy bar data out of
    the agent's context. During the session it includes today's in-progress bar.
  * Raw Robinhood MCP `get_equity_historicals` results saved to files by the agent (fallback).

Prices from here only pick candidates. Every order is re-checked against a live broker quote
before it is released (see engine.next_order), so a bad bar here cannot place a trade by itself.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = "Mozilla/5.0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "data", "cache")


@dataclass
class Series:
    symbol: str
    dates: List[str] = field(default_factory=list)  # exchange-local YYYY-MM-DD
    open: List[float] = field(default_factory=list)
    high: List[float] = field(default_factory=list)
    low: List[float] = field(default_factory=list)
    close: List[float] = field(default_factory=list)
    volume: List[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.dates)

    def append(self, date: str, o: float, h: float, l: float, c: float, v: float) -> None:
        self.dates.append(date)
        self.open.append(o)
        self.high.append(h)
        self.low.append(l)
        self.close.append(c)
        self.volume.append(v)

    def index_of(self, date: str) -> Optional[int]:
        if not hasattr(self, "_idx") or len(self._idx) != len(self.dates):
            self._idx = {d: i for i, d in enumerate(self.dates)}
        return self._idx.get(date)

    def truncated(self, last_date: str) -> "Series":
        """A copy holding only bars dated on or before last_date."""
        out = Series(self.symbol)
        for i, d in enumerate(self.dates):
            if d > last_date:
                break
            out.append(d, self.open[i], self.high[i], self.low[i], self.close[i], self.volume[i])
        return out

    def with_last_price(self, date: str, price: float) -> "Series":
        """A copy whose bar for `date` reflects a live price.

        If the last bar is already dated `date` its close is replaced (and high/low widened);
        otherwise a new provisional bar is appended.
        """
        out = self.truncated("9999-12-31")
        if out.dates and out.dates[-1] == date:
            out.close[-1] = price
            out.high[-1] = max(out.high[-1], price)
            out.low[-1] = min(out.low[-1], price)
        elif not out.dates or out.dates[-1] < date:
            out.append(date, price, price, price, price, 0.0)
        return out

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "dates": self.dates,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Series":
        return cls(d["symbol"], list(d["dates"]), list(d["open"]), list(d["high"]),
                   list(d["low"]), list(d["close"]), list(d["volume"]))


# --------------------------------------------------------------------------- Yahoo

def fetch_yahoo(symbol: str, start: Optional[str] = None, days: int = 420,
                timeout: float = 20.0, retries: int = 3) -> Series:
    """Fetch daily bars. `start` (YYYY-MM-DD) wins over `days` (calendar days back from now)."""
    now = int(time.time())
    if start:
        period1 = int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    else:
        period1 = now - days * 86400
    query = urllib.parse.urlencode({
        "period1": period1, "period2": now + 86400, "interval": "1d", "events": "div,splits",
    })
    url = YAHOO_URL.format(symbol=urllib.parse.quote(symbol)) + "?" + query
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.load(resp)
            return parse_yahoo(symbol, payload)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, OSError) as err:
            last_err = err
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"yahoo fetch failed for {symbol}: {last_err!r}")


def parse_yahoo(symbol: str, payload: dict) -> Series:
    result = payload["chart"]["result"][0]
    stamps = result.get("timestamp") or []
    quote = result["indicators"]["quote"][0]
    offset = int(result.get("meta", {}).get("gmtoffset", -18000))
    series = Series(symbol)
    for i, ts in enumerate(stamps):
        o, h, l, c = quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i]
        v = quote["volume"][i]
        if None in (o, h, l, c) or c <= 0:
            continue
        date = (datetime.fromtimestamp(ts, timezone.utc) + timedelta(seconds=offset)).strftime("%Y-%m-%d")
        if series.dates and series.dates[-1] == date:
            continue
        series.append(date, float(o), float(max(h, o, c)), float(min(l, o, c)), float(c), float(v or 0))
    return series


# --------------------------------------------------------------------------- Robinhood MCP files

def parse_mcp_historicals(payload: dict) -> Dict[str, Series]:
    """Parse a raw `get_equity_historicals` result (interval=day) into Series per symbol."""
    out: Dict[str, Series] = {}
    results = payload.get("data", payload).get("results", [])
    for res in results:
        series = Series(res["symbol"])
        for bar in res.get("bars", []):
            if bar.get("interpolated"):
                continue
            series.append(
                bar["begins_at"][:10],
                float(bar["open_price"]), float(bar["high_price"]),
                float(bar["low_price"]), float(bar["close_price"]),
                float(bar.get("volume") or 0),
            )
        out[series.symbol] = series
    return out


def load_mcp_files(paths: Iterable[str]) -> Dict[str, Series]:
    out: Dict[str, Series] = {}
    for path in paths:
        with open(path, "r", encoding="utf-8") as fh:
            out.update(parse_mcp_historicals(json.load(fh)))
    return out


# --------------------------------------------------------------------------- cache + bulk load

def _cache_path(symbol: str) -> str:
    return os.path.join(CACHE_DIR, f"{symbol.replace('^', '_')}.json")


def load_cached(symbol: str) -> Optional[Series]:
    path = _cache_path(symbol)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return Series.from_dict(json.load(fh))


def save_cached(series: Series) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_cache_path(series.symbol), "w", encoding="utf-8") as fh:
        json.dump(series.to_dict(), fh)


def load_universe(symbols: Iterable[str], start: Optional[str] = None, days: int = 420,
                  use_cache: bool = False, workers: int = 6) -> tuple[Dict[str, Series], Dict[str, str]]:
    """Fetch every symbol. Returns (series by symbol, error text by symbol)."""
    from concurrent.futures import ThreadPoolExecutor

    data: Dict[str, Series] = {}
    errors: Dict[str, str] = {}
    todo: List[str] = []
    for symbol in dict.fromkeys(symbols):
        cached = load_cached(symbol) if use_cache else None
        if cached is not None:
            data[symbol] = cached
        else:
            todo.append(symbol)

    def fetch(symbol: str):
        try:
            series = fetch_yahoo(symbol, start=start, days=days)
            if len(series) == 0:
                raise RuntimeError("no bars returned")
            return symbol, series, None
        except Exception as err:  # noqa: BLE001 - one bad symbol must not sink the cycle
            return symbol, None, str(err)

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for symbol, series, err in pool.map(fetch, todo):
                if series is None:
                    errors[symbol] = err or "unknown error"
                    continue
                data[symbol] = series
                if use_cache:
                    save_cached(series)
    return data, errors
