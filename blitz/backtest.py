"""Portfolio backtest on daily bars, using the same setups, exits and sizing as the live engine.

Modelling choices (all conservative or stated):
  * Entries fill at the signal bar's close plus slippage ("close" mode, matching a live run a few
    minutes before the bell) or at the next bar's open ("next_open" mode).
  * Stops: "close" (the live behaviour) judges the stop on the close, and keeps only a wider
    disaster stop live intraday; "intraday" exits whenever the day's low touches the stop.
    Intraday stop fills take an extra penalty (the open if the bar gaps below).
  * If a stop and a target are both touched in one bar, the stop is assumed to have come first.
  * Slippage is charged on every fill. Robinhood charges no commission.
  * No earnings filter (no reliable history here), so results include earnings gaps both ways.

Run:  python -m blitz.backtest --help
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from typing import Dict, List, Optional

from .config import load_config
from .data import Series, load_universe
from .sizing import disaster_stop_price, position_dollars
from .strategies import Ind, Signal, exit_reason, find_signals, momentum_ranks


@dataclass
class Pos:
    symbol: str
    setup: str
    entry_date: str
    entry_i: int
    entry_price: float
    qty: float
    stop: float
    target: Optional[float]
    disaster: float = 0.0


def run_backtest(data: Dict[str, Series], cfg: dict, start: str, end: str, initial: float = 1000.0,
                 entry_mode: str = "close", stop_mode: str = "close", slippage_bps: float = 5.0,
                 soft_stop_penalty: float = 0.005, universe: Optional[List[str]] = None) -> dict:
    regime_cfg = cfg.get("regime", {})
    cal_symbol = regime_cfg.get("symbol", "SPY")
    calendar = [d for d in data[cal_symbol].dates if start <= d <= end]
    symbols = [s for s in (universe or cfg["universe"]["symbols"]) if s in data]
    inds = {s: Ind(data[s]) for s in set(symbols) | {cal_symbol}}
    setups_cfg = cfg["setups"]
    common = cfg.get("common", {})
    sizing = cfg["sizing"]
    slip = slippage_bps / 10_000.0
    priority = cfg.get("setup_priority", ["dip", "gap", "breakout"])
    need_mom = any(sc.get("enabled") and sc.get("min_momentum_rank") for sc in setups_cfg.values())
    max_new_per_day = cfg.get("execution", {}).get("max_new_positions_per_day", 99)
    group_of = {sym: g for g, members in cfg["universe"].get("groups", {}).items() for sym in members}
    max_per_group = sizing.get("max_positions_per_group", 99)

    cash = initial
    positions: Dict[str, Pos] = {}
    pending: List[Signal] = []
    locks: Dict[str, int] = {}
    trades: List[dict] = []
    equity_curve: List[tuple] = []
    exposure_sum = 0.0
    last_close: Dict[str, float] = {}

    def equity_now() -> float:
        return cash + sum(p.qty * last_close.get(p.symbol, p.entry_price) for p in positions.values())

    def open_position(sig: Signal, fill: float, i: int, date: str, size_mult: float) -> None:
        nonlocal cash
        if sig.symbol in positions or len(positions) >= sizing["max_positions"]:
            return
        eq = equity_now()
        invested = eq - cash
        room = eq * sizing.get("max_exposure_pct", 1.0) - invested
        stop_dist = max(1e-6, (fill - sig.stop) / fill)
        dollars = min(position_dollars(eq, stop_dist, sizing, size_mult), room, cash)
        if dollars < max(1.0, eq * 0.02):
            return
        qty = dollars / fill
        cash -= dollars
        positions[sig.symbol] = Pos(sig.symbol, sig.setup, date, i, fill, qty, sig.stop, sig.target,
                                    disaster_stop_price(fill, stop_dist, cfg.get("checks", {})))
        last_close[sig.symbol] = fill

    def close_position(pos: Pos, price: float, date: str, i: int, reason: str) -> None:
        nonlocal cash
        fill = price * (1.0 - slip)
        cash += pos.qty * fill
        trades.append({
            "symbol": pos.symbol, "setup": pos.setup, "entry_date": pos.entry_date, "exit_date": date,
            "entry": pos.entry_price, "exit": fill, "ret": fill / pos.entry_price - 1.0,
            "pnl": pos.qty * (fill - pos.entry_price), "bars": i - pos.entry_i, "reason": reason,
        })
        del positions[pos.symbol]
        if "stop" in reason or "disaster" in reason:
            locks[pos.symbol] = cfg.get("locks", {}).get("after_stop_days", 3)

    for day_n, date in enumerate(calendar):
        for sym in list(locks):
            locks[sym] -= 1
            if locks[sym] <= 0:
                del locks[sym]

        reg_ind = inds[cal_symbol]
        ri = reg_ind.s.index_of(date)
        bull = True
        if regime_cfg.get("enabled", True) and ri is not None:
            r_sma = reg_ind.sma(regime_cfg.get("sma", 200))[ri]
            bull = r_sma is None or reg_ind.s.close[ri] > r_sma
        size_mult = 1.0 if bull else regime_cfg.get("bear_size_mult", 0.5)
        blocked = set() if bull else set(regime_cfg.get("bear_blocks", ["breakout", "gap"]))

        # 1. Fill entries queued from yesterday's close at today's open.
        opened_today = set()
        for sig in pending:
            ind = inds[sig.symbol]
            i = ind.s.index_of(date)
            if i is None:
                continue
            o = ind.s.open[i]
            max_gap = setups_cfg[sig.setup].get("max_entry_gap_pct", 0.03)
            if o > sig.price * (1.0 + max_gap) or o <= sig.stop:
                continue
            before = len(positions)
            open_position(sig, o * (1.0 + slip), i, date, size_mult)
            if len(positions) > before:
                opened_today.add(sig.symbol)
        pending = []

        # 2. Manage open positions.
        for pos in list(positions.values()):
            ind = inds[pos.symbol]
            i = ind.s.index_of(date)
            if i is None:
                continue
            s = ind.s
            bars_held = i - pos.entry_i
            live_today = bars_held > 0 or pos.symbol in opened_today
            if not live_today:
                continue
            o, h, l, c = s.open[i], s.high[i], s.low[i], s.close[i]
            if stop_mode == "intraday":
                if o <= pos.stop and pos.symbol not in opened_today:
                    close_position(pos, o, date, i, "stop_gap")
                    continue
                if l <= pos.stop:
                    close_position(pos, pos.stop * (1.0 - soft_stop_penalty), date, i, "stop")
                    continue
            else:
                # Stop judged at the close; only the wider disaster stop is live intraday.
                if o <= pos.disaster and pos.symbol not in opened_today:
                    close_position(pos, o, date, i, "disaster_gap")
                    continue
                if l <= pos.disaster:
                    close_position(pos, pos.disaster * (1.0 - soft_stop_penalty), date, i, "disaster_stop")
                    continue
                if c <= pos.stop:
                    close_position(pos, c, date, i, "stop_close")
                    continue
            if pos.target is not None and h >= pos.target:
                close_position(pos, max(o, pos.target) if pos.symbol not in opened_today else pos.target,
                               date, i, "target")
                continue
            reason = exit_reason(pos.setup, ind, i, pos.entry_price, bars_held, setups_cfg[pos.setup])
            if reason:
                close_position(pos, c, date, i, reason)
                continue
            last_close[pos.symbol] = c

        # 3. Look for new entries on today's close.
        if len(positions) < sizing["max_positions"]:
            ranks = momentum_ranks({s: inds[s] for s in symbols}, date) if need_mom else {}
            found: List[Signal] = []
            for sym in symbols:
                if sym in positions or sym in locks:
                    continue
                ind = inds[sym]
                i = ind.s.index_of(date)
                if i is None:
                    continue
                for sig in find_signals(ind, i, setups_cfg, common):
                    if sig.setup in blocked:
                        continue
                    min_rank = setups_cfg[sig.setup].get("min_momentum_rank")
                    if min_rank and ranks.get(sym, 0.0) < min_rank:
                        continue
                    found.append(sig)
            found.sort(key=lambda g: (priority.index(g.setup) if g.setup in priority else 99, -g.score))
            seen = set()
            new_today = 0
            for sig in found:
                if sig.symbol in seen:
                    continue
                seen.add(sig.symbol)
                if len(positions) + len(pending) >= sizing["max_positions"] or new_today >= max_new_per_day:
                    break
                grp = group_of.get(sig.symbol)
                if grp is not None:
                    held = sum(1 for s2 in list(positions) + [p.symbol for p in pending] if group_of.get(s2) == grp)
                    if held >= max_per_group:
                        continue
                if entry_mode == "close":
                    before = len(positions)
                    open_position(sig, sig.price * (1.0 + slip), inds[sig.symbol].s.index_of(date), date, size_mult)
                    new_today += len(positions) - before
                else:
                    pending.append(sig)
                    new_today += 1

        eq = equity_now()
        equity_curve.append((date, eq))
        exposure_sum += (eq - cash) / eq if eq > 0 else 0.0

    return summarize(trades, equity_curve, initial, exposure_sum / max(1, len(calendar)))


def summarize(trades: List[dict], curve: List[tuple], initial: float, avg_exposure: float) -> dict:
    out: dict = {"trades": len(trades), "avg_exposure": round(avg_exposure, 3)}
    if not curve:
        return out
    final = curve[-1][1]
    years = max(1e-9, len(curve) / 252.0)
    out["total_return"] = round(final / initial - 1.0, 4)
    out["cagr"] = round((final / initial) ** (1.0 / years) - 1.0, 4) if final > 0 else -1.0
    peak, max_dd = initial, 0.0
    rets = []
    prev = initial
    for _, eq in curve:
        peak = max(peak, eq)
        max_dd = max(max_dd, 1.0 - eq / peak)
        rets.append(eq / prev - 1.0)
        prev = eq
    out["max_drawdown"] = round(max_dd, 4)
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    out["sharpe"] = round(mean / math.sqrt(var) * math.sqrt(252), 2) if var > 0 else 0.0
    out["trades_per_year"] = round(len(trades) / years, 1)
    if trades:
        wins = [t for t in trades if t["pnl"] > 0]
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
        out["win_rate"] = round(len(wins) / len(trades), 3)
        out["avg_trade_ret"] = round(sum(t["ret"] for t in trades) / len(trades), 4)
        out["profit_factor"] = round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf")
        out["avg_bars_held"] = round(sum(t["bars"] for t in trades) / len(trades), 2)
        out["pct_held_3d_or_less"] = round(sum(1 for t in trades if t["bars"] <= 3) / len(trades), 3)
        out["worst_trade"] = round(min(t["ret"] for t in trades), 4)
        by_setup: Dict[str, List[dict]] = {}
        for t in trades:
            by_setup.setdefault(t["setup"], []).append(t)
        out["by_setup"] = {
            k: {"trades": len(v), "win_rate": round(sum(1 for t in v if t["pnl"] > 0) / len(v), 3),
                "avg_ret": round(sum(t["ret"] for t in v) / len(v), 4),
                "pnl": round(sum(t["pnl"] for t in v), 2)}
            for k, v in by_setup.items()
        }
        by_reason: Dict[str, int] = {}
        for t in trades:
            by_reason[t["reason"]] = by_reason.get(t["reason"], 0) + 1
        out["exit_reasons"] = by_reason
    by_year: Dict[str, List[float]] = {}
    prev = initial
    for date, eq in curve:
        by_year.setdefault(date[:4], [prev, eq])[1] = eq
        prev = eq
    out["by_year"] = {y: round(v[1] / v[0] - 1.0, 4) for y, v in by_year.items()}
    return out


def buy_and_hold(series: Series, start: str, end: str) -> dict:
    pts = [(d, c) for d, c in zip(series.dates, series.close) if start <= d <= end]
    if not pts:
        return {}
    return summarize([], pts, pts[0][1], 1.0)


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Backtest the BlitzTrader setups on daily bars.")
    ap.add_argument("--config", default=None, help="path to risk_rules.json (default: repo root)")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2099-12-31")
    ap.add_argument("--data-start", default="2014-06-01", help="first bar to download (indicator warm-up)")
    ap.add_argument("--entry", choices=["close", "next_open"], default="close")
    ap.add_argument("--stop-mode", choices=["intraday", "close"], default="close")
    ap.add_argument("--slippage-bps", type=float, default=5.0)
    ap.add_argument("--initial", type=float, default=1000.0)
    ap.add_argument("--refresh", action="store_true", help="re-download instead of using data/cache")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    symbols = sorted(set(cfg["universe"]["symbols"]) | {cfg.get("regime", {}).get("symbol", "SPY")})
    if args.refresh:
        from .data import CACHE_DIR
        import os
        import shutil
        if os.path.isdir(CACHE_DIR):
            shutil.rmtree(CACHE_DIR)
    data, errors = load_universe(symbols, start=args.data_start, use_cache=True)
    result = run_backtest(data, cfg, args.start, args.end, initial=args.initial, entry_mode=args.entry,
                          stop_mode=args.stop_mode, slippage_bps=args.slippage_bps)
    result["benchmark_spy"] = buy_and_hold(data["SPY"], args.start, args.end)
    result["data_errors"] = errors
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
