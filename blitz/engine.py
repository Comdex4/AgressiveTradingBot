"""The decision engine: sync -> plan -> (release -> fill)* -> report.

The agent session supplies broker facts and places orders; every decision is made here.

  sync     Record the broker snapshot and reconcile it against the bot's own ledger.
  plan     Decide exits and entries for this session and queue them, sells first.
  release  The check that runs between trades. One order at a time: it re-verifies the order
           against a live broker quote and the current budget, and refuses to release the next
           order until the previous one has been resolved with `fill`.
  fill     Record what the broker actually did with a released order.
  report   Mark the book, update circuit-breaker state, write state/STATUS.md.
"""

from __future__ import annotations

import math
import os
from datetime import date, datetime
from typing import Callable, Dict, List, Optional

from . import ledger as lg
from .clock import add_trading_days, is_trading_day, trading_days_between, window_status
from .data import Series, load_universe
from .sizing import disaster_stop_price, position_dollars
from .strategies import Ind, exit_reason, find_signals, momentum_ranks

EPS = 1e-6
FINAL_STATES = ("filled", "skipped", "rejected", "cancelled", "failed")


# --------------------------------------------------------------------------- mode

def effective_mode(cfg: dict) -> tuple:
    """(mode, note). Live trading is refused until enough paper cycles are on record."""
    if cfg["execution"]["mode"] != "live":
        return "paper", None
    need = cfg["execution"].get("paper_min_cycle_days_before_live", 5)
    have = len(set(lg.load("paper")["cycle_dates"]))
    if have < need:
        return "paper", (f"execution.mode is 'live' but only {have} of the required {need} paper "
                         "cycle days are on record, so this run stays in paper mode")
    return "live", None


# --------------------------------------------------------------------------- book maths

def sleeve_equity(cfg: dict, led: dict) -> float:
    broker = led.get("broker") or {}
    return cfg["sleeve"]["allocation_pct"] * float(broker.get("total_value", 0.0))


def _price(led: dict, sym: str) -> float:
    pos = led["positions"][sym]
    return float(led["last_prices"].get(sym, pos["avg_cost"]))


def open_value(led: dict, exclude: Optional[set] = None) -> float:
    return sum(p["qty"] * _price(led, s) for s, p in led["positions"].items()
               if not exclude or s not in exclude)


def unrealized_pnl(led: dict) -> float:
    return sum(p["qty"] * (_price(led, s) - p["avg_cost"]) for s, p in led["positions"].items())


def total_pnl(led: dict) -> float:
    return led["realized_pnl"] + unrealized_pnl(led)


# --------------------------------------------------------------------------- circuit breakers

def evaluate_breakers(cfg: dict, led: dict, today: str) -> dict:
    """Should new entries be blocked or shrunk? Exits are never blocked."""
    limits = cfg["loss_limits"]
    eq = sleeve_equity(cfg, led)
    now_pnl = total_pnl(led)
    reasons: List[str] = []
    size_mult = 1.0
    prior = [m for m in led["marks"] if m["date"] < today]
    daily = now_pnl - prior[-1]["total_pnl"] if prior else now_pnl
    week_ref = prior[-5]["total_pnl"] if len(prior) >= 5 else (prior[0]["total_pnl"] if prior else 0.0)
    weekly = now_pnl - week_ref
    peak = max([led.get("peak_pnl", 0.0), now_pnl])
    drawdown = (peak - now_pnl) / eq if eq > 0 else 0.0
    if lg.is_halted():
        reasons.append("manual HALT file is present")
    if eq > 0 and daily <= -limits.get("daily_loss_pct", 1.0) * eq:
        reasons.append(f"daily loss limit hit ({daily:+.2f})")
    if eq > 0 and weekly <= -limits.get("weekly_loss_pct", 1.0) * eq:
        reasons.append(f"weekly loss limit hit ({weekly:+.2f})")
    if drawdown >= limits.get("drawdown_halt_pct", 1.0):
        reasons.append(f"drawdown halt ({drawdown:.1%} of sleeve); a human must run `reset-drawdown`")
    elif drawdown >= limits.get("drawdown_half_size_pct", 1.0):
        size_mult = 0.5
    cooldown = led.get("cooldown_until")
    if cooldown and today <= cooldown:
        reasons.append(f"loss-streak cooldown through {cooldown}")
    return {
        "block_entries": bool(reasons), "reasons": reasons, "size_mult": size_mult,
        "daily_pnl": round(daily, 2), "weekly_pnl": round(weekly, 2), "drawdown_pct": round(drawdown, 4),
    }


# --------------------------------------------------------------------------- sync

def sync(cfg: dict, led: dict, total_value: float, buying_power: float,
         broker_positions: Dict[str, float], now: datetime) -> dict:
    today = now.date().isoformat()
    incidents: List[dict] = []
    # A sell that was released but never recorded explains a missing position: leave it for
    # `plan` to flag (resolve_pending) instead of writing it off as someone else's sale.
    in_flight = {i["symbol"] for i in (led.get("queue") or {}).get("items", []) if i["status"] == "released"}
    if led["mode"] == "live":
        for sym in list(led["positions"]):
            pos = led["positions"][sym]
            have = float(broker_positions.get(sym, 0.0))
            if have + EPS >= pos["qty"] or sym in in_flight:
                continue
            # Someone else (another bot, or the owner) sold shares this ledger thought it owned.
            incident = {"date": today, "type": "external_reduction", "symbol": sym,
                        "ledger_qty": pos["qty"], "broker_qty": have}
            incidents.append(incident)
            if have <= EPS:
                led["closed_trades"].append({
                    "symbol": sym, "setup": pos["setup"], "entry_date": pos["entry_date"],
                    "exit_date": today, "entry": pos["avg_cost"], "exit": None, "qty": pos["qty"],
                    "pnl": 0.0, "reason": "external_close_pnl_unknown"})
                del led["positions"][sym]
            else:
                pos["qty"] = have
    led["incidents"].extend(incidents)
    foreign = sorted(s for s, q in broker_positions.items() if q > EPS and s not in led["positions"])
    led["broker"] = {
        "ts": now.isoformat(timespec="seconds"), "date": today,
        "total_value": float(total_value), "buying_power": float(buying_power),
        "positions": {s: float(q) for s, q in broker_positions.items()}, "foreign": foreign,
    }
    eq = sleeve_equity(cfg, led)
    lg.journal("sync", {"mode": led["mode"], "total_value": total_value, "buying_power": buying_power,
                        "sleeve_equity": round(eq, 2), "incidents": incidents}, now)
    return {
        "ok": True, "mode": led["mode"], "date": today,
        "sleeve_equity": round(eq, 2),
        "sleeve_deployed": round(open_value(led), 2),
        "sleeve_positions": sorted(led["positions"]),
        "foreign_symbols_excluded": foreign,
        "incidents": incidents,
        "next_step": "Run `plan`. For a pulse session, or whenever positions are open, pass a broker "
                     "quote for each sleeve position: --quote SYM=bid:ask:last",
    }


# --------------------------------------------------------------------------- plan

def _locked(led: dict, sym: str, today: str) -> bool:
    until = led["locks"].get(sym)
    return bool(until and today <= until)


def _floor_cents(x: float) -> float:
    return math.floor(x * 100 + 1e-9) / 100.0


def plan(cfg: dict, led: dict, session: str, now: datetime,
         data: Optional[Dict[str, Series]] = None, quotes: Optional[Dict[str, dict]] = None,
         earnings: Optional[Dict[str, str]] = None,
         loader: Callable = load_universe) -> dict:
    today = now.date().isoformat()
    broker = led.get("broker")
    if not broker or broker.get("date") != today:
        return {"ok": False, "error": "no broker snapshot for today", "next_step": "Run `sync` first."}
    queue = led.get("queue")
    if queue:
        stuck = [i for i in queue["items"] if i["status"] == "released"]
        if stuck:
            return {"ok": False, "action": "resolve_pending", "items": stuck,
                    "next_step": "An order was released earlier but never resolved. Look it up with "
                                 "get_equity_orders (same symbol, side and day) and record the outcome "
                                 "with `fill --id N --status ...` before planning again."}
    if not is_trading_day(now.date(), cfg):
        led["queue"] = {"date": today, "session": session, "items": [], "cash_delta": 0.0, "context": {}}
        return {"ok": True, "orders": [], "notes": ["market is closed today"], "next_step": "Run `report`."}

    quotes = quotes or {}
    earnings = earnings or {}
    checks = cfg["checks"]
    setups_cfg = cfg["setups"]
    regime_cfg = cfg.get("regime", {})
    regime_symbol = regime_cfg.get("symbol", "SPY")
    notes: List[str] = []
    errors: Dict[str, str] = {}

    if data is None:
        wanted = set(led["positions"]) | {regime_symbol}
        if session == "close":
            wanted |= set(cfg["universe"]["symbols"])
        data, errors = loader(sorted(wanted), days=420)

    # Current prices of our own positions: a broker quote if supplied, else today's bar.
    prices: Dict[str, float] = {}
    for sym in led["positions"]:
        if sym in quotes:
            prices[sym] = quotes[sym].get("last") or (quotes[sym]["bid"] + quotes[sym]["ask"]) / 2.0
        elif sym in data and len(data[sym]) and data[sym].dates[-1] == today:
            prices[sym] = data[sym].close[-1]
    led["last_prices"].update(prices)

    items: List[dict] = []
    next_day = add_trading_days(now.date(), 1, cfg).isoformat()

    # ---- exits (always first)
    for sym in sorted(led["positions"]):
        pos = led["positions"][sym]
        price = prices.get(sym)
        if price is None:
            notes.append(f"{sym}: no fresh price, exit rules not evaluated; re-run plan with --quote {sym}=bid:ask:last")
            continue
        if pos["entry_date"] == today and not checks.get("allow_same_day_exit", False):
            continue
        setup_cfg = setups_cfg[pos["setup"]]
        bars_held = trading_days_between(date.fromisoformat(pos["entry_date"]), now.date(), cfg)
        reason = None
        if price <= pos["disaster_stop"]:
            reason = "disaster_stop"
        elif session == "close":
            ed = earnings.get(sym)
            if price <= pos["stop"]:
                reason = "stop_close"
            elif checks.get("exit_before_earnings", True) and ed and today <= ed <= next_day:
                reason = "earnings_exit"
            elif sym in data and len(data[sym]):
                series = data[sym].with_last_price(today, price)
                reason = exit_reason(pos["setup"], Ind(series), len(series) - 1, pos["avg_cost"],
                                     bars_held, setup_cfg)
            elif bars_held >= setup_cfg.get("max_hold_days", 3):
                reason = "time_stop"
        if reason:
            items.append({
                "id": len(items) + 1, "symbol": sym, "side": "sell", "setup": pos["setup"],
                "reason": reason, "plan_price": round(price, 4), "qty": pos["qty"],
                "status": "pending", "bars_held": bars_held,
            })

    # ---- entries (close session only)
    breakers = evaluate_breakers(cfg, led, today)
    bull = True
    reg = data.get(regime_symbol)
    data_fresh = bool(reg and len(reg) and reg.dates[-1] == today)
    if reg and len(reg):
        r_ind = Ind(reg)
        r_sma = r_ind.sma(regime_cfg.get("sma", 200))[-1]
        bull = (not regime_cfg.get("enabled", True)) or r_sma is None or reg.close[-1] > r_sma
    watch: List[dict] = []
    if session == "close":
        in_window, why = window_status(now, cfg, entry=True)
        selling = {i["symbol"] for i in items}
        held_after = [s for s in led["positions"] if s not in selling]
        sizing = cfg["sizing"]
        entered_today = sum(1 for p in led["positions"].values() if p["entry_date"] == today)
        slots = min(sizing["max_positions"] - len(held_after),
                    cfg["execution"].get("max_new_positions_per_day", 99) - entered_today)
        if not in_window:
            notes.append(f"no entries: {why}")
        elif breakers["block_entries"]:
            notes.append("no entries: " + "; ".join(breakers["reasons"]))
        elif not data_fresh:
            notes.append(f"no entries: {regime_symbol} has no bar for today, market data is stale or the market is closed")
        elif slots <= 0:
            notes.append("no entries: all position slots are in use")
        else:
            blocked = set() if bull else set(regime_cfg.get("bear_blocks", []))
            size_mult = (1.0 if bull else regime_cfg.get("bear_size_mult", 0.5)) * breakers["size_mult"]
            inds = {s: Ind(data[s]) for s in cfg["universe"]["symbols"]
                    if s in data and len(data[s]) and data[s].dates[-1] == today}
            ranks = momentum_ranks(inds, today)
            foreign = set(broker.get("foreign", [])) if cfg["universe"].get("exclude_foreign_holdings", True) else set()
            found = []
            for sym, ind in inds.items():
                if sym in led["positions"] or sym in foreign or _locked(led, sym, today):
                    continue
                for sig in find_signals(ind, len(ind.s) - 1, setups_cfg, cfg.get("common", {})):
                    if sig.setup in blocked:
                        continue
                    min_rank = setups_cfg[sig.setup].get("min_momentum_rank")
                    if min_rank and ranks.get(sym, 0.0) < min_rank:
                        continue
                    found.append(sig)
            priority = cfg.get("setup_priority", [])
            found.sort(key=lambda g: (priority.index(g.setup) if g.setup in priority else 99, -g.score))
            eq = sleeve_equity(cfg, led)
            budget = eq * sizing.get("max_exposure_pct", 1.0) - open_value(led, exclude=selling)
            cash = broker["buying_power"] - cfg["sleeve"].get("cash_buffer_dollars", 0.0) \
                + sum(i["qty"] * i["plan_price"] for i in items)
            seen = set()
            for sig in found:
                if sig.symbol in seen:
                    continue
                seen.add(sig.symbol)
                info = {"symbol": sig.symbol, "setup": sig.setup, "price": round(sig.price, 4),
                        "momentum_rank": round(ranks.get(sig.symbol, 0.0), 2), **sig.notes}
                if slots <= 0:
                    watch.append({**info, "not_taken": "no free slot"})
                    continue
                dollars = _floor_cents(min(position_dollars(eq, sig.stop_dist_pct, sizing, size_mult), budget, cash))
                if dollars < cfg["sleeve"].get("min_order_dollars", 1.0):
                    watch.append({**info, "not_taken": "sleeve budget or buying power exhausted"})
                    continue
                items.append({
                    "id": len(items) + 1, "symbol": sig.symbol, "side": "buy", "setup": sig.setup,
                    "reason": "entry", "plan_price": round(sig.price, 4), "dollars": dollars,
                    "stop_dist_pct": round(sig.stop_dist_pct, 5), "atr": round(sig.atr, 4),
                    "notes": info, "status": "pending",
                })
                budget -= dollars
                cash -= dollars
                slots -= 1

    max_orders = cfg["execution"].get("max_orders_per_cycle", 12)
    if len(items) > max_orders:
        notes.append(f"queue trimmed to {max_orders} orders")
        items = items[:max_orders]
    for item in items:
        item["ref_id"] = f"blitz-{led['mode']}-{today}-{session}-{item['id']}-{item['symbol']}-{item['side']}"

    context = {"regime": "bull" if bull else "bear", "breakers": breakers,
               "sleeve_equity": round(sleeve_equity(cfg, led), 2), "data_errors": errors}
    led["queue"] = {"date": today, "session": session, "created": now.isoformat(timespec="seconds"),
                    "items": items, "cash_delta": 0.0, "context": context}
    lg.journal("plan", {"mode": led["mode"], "session": session, "orders": items, "notes": notes,
                        "context": context, "watch": watch[:10]}, now)
    buys = [i["symbol"] for i in items if i["side"] == "buy"]
    step = ("No orders. Run `report`." if not items else
            "Work the orders strictly in id order. For each: get a fresh broker quote"
            + (" and, for buys, the next earnings date" if buys else "")
            + ", call review_equity_order with the order's parameters, then run "
              "`release --id N --quote bid:ask:last --review ok|alert`"
            + (" `--earnings YYYY-MM-DD|none|unknown`" if buys else "") + ". Finish with `report`.")
    return {"ok": True, "mode": led["mode"], "session": session, "date": today,
            "regime": context["regime"], "breakers": breakers, "sleeve_equity": context["sleeve_equity"],
            "orders": [_order_view(i) for i in items], "watch": watch[:10], "notes": notes,
            "data_errors": errors, "next_step": step}


def _order_view(item: dict) -> dict:
    view = {"id": item["id"], "symbol": item["symbol"], "side": item["side"], "setup": item["setup"],
            "reason": item["reason"], "plan_price": item["plan_price"], "status": item["status"],
            "ref_id": item.get("ref_id"), "broker_order": _broker_order(item)}
    if item["side"] == "buy":
        view["notes"] = item.get("notes")
    return view


def _broker_order(item: dict) -> dict:
    """Exact parameters for review_equity_order / place_equity_order (account_number added by the agent)."""
    order = {"symbol": item["symbol"], "side": item["side"], "type": "market",
             "time_in_force": "gfd", "market_hours": "regular_hours"}
    if item["side"] == "buy":
        order["dollar_amount"] = f"{item['dollars']:.2f}"
    else:
        order["quantity"] = f"{item['qty']:.6f}".rstrip("0").rstrip(".")
    return order


# --------------------------------------------------------------------------- release (between-trade checks)

def _find(led: dict, item_id: int) -> Optional[dict]:
    queue = led.get("queue") or {}
    for item in queue.get("items", []):
        if item["id"] == item_id:
            return item
    return None


def release(cfg: dict, led: dict, item_id: int, quote: dict, review: str, now: datetime,
            earnings: str = "unknown", alert_text: str = "") -> dict:
    today = now.date().isoformat()
    queue = led.get("queue")
    item = _find(led, item_id)
    if not queue or item is None:
        return {"ok": False, "error": f"no queued order with id {item_id}", "next_step": "Run `plan`."}
    if queue["date"] != today:
        return {"ok": False, "error": "the queue is from another day", "next_step": "Run `sync` and `plan` again."}
    if item["status"] != "pending":
        return {"ok": False, "error": f"order {item_id} is already {item['status']}"}
    waiting = [i["id"] for i in queue["items"] if i["status"] == "released"]
    if waiting:
        return {"ok": False, "error": f"order {waiting[0]} was released and has not been resolved",
                "next_step": f"Confirm it with get_equity_orders and run `fill --id {waiting[0]} ...` first."}
    earlier = [i["id"] for i in queue["items"] if i["id"] < item_id and i["status"] == "pending"]
    if earlier:
        return {"ok": False, "error": f"order {earlier[0]} comes first", "next_step": "Work the orders in id order."}

    checks_cfg = cfg["checks"]
    buy = item["side"] == "buy"
    results: List[dict] = []

    def check(name: str, ok: bool, detail: str = "", hard: bool = True) -> None:
        results.append({"check": name, "ok": bool(ok), "detail": detail, "blocks": hard and not ok})

    in_window, why = window_status(now, cfg, entry=buy)
    check("trading_window", in_window, why)

    bid, ask, last = quote.get("bid", 0.0), quote.get("ask", 0.0), quote.get("last", 0.0)
    sane = bid > 0 and ask >= bid
    check("quote_sane", sane, f"bid={bid} ask={ask} last={last}")
    mid = (bid + ask) / 2.0 if sane else (last or item["plan_price"])
    spread = (ask - bid) / mid if sane and mid > 0 else 1.0
    # A wide spread blocks a buy. It only warns on a sell: getting out matters more than the fill.
    check("spread", spread <= checks_cfg.get("max_spread_pct", 0.002),
          f"{spread:.4%} (max {checks_cfg.get('max_spread_pct', 0.002):.4%})", hard=buy)
    check("broker_review", review == "ok", alert_text or review)

    dollars = item.get("dollars", 0.0)
    if buy:
        drift = abs(mid - item["plan_price"]) / item["plan_price"]
        check("price_matches_plan", drift <= checks_cfg.get("max_quote_drift_pct", 0.02),
              f"broker mid {mid:.4f} vs plan {item['plan_price']:.4f} ({drift:.2%})")
        check("manual_halt", not lg.is_halted(), "state/HALT present" if lg.is_halted() else "no halt")
        breakers = evaluate_breakers(cfg, led, today)
        check("circuit_breakers", not breakers["block_entries"], "; ".join(breakers["reasons"]))
        window_days = checks_cfg.get("earnings_blackout_trading_days", 3)
        if earnings in ("none", "unknown"):
            check("earnings_blackout", True, f"earnings date {earnings}")
        else:
            last_blackout = add_trading_days(now.date(), window_days, cfg).isoformat()
            check("earnings_blackout", not (today <= earnings <= last_blackout),
                  f"earnings {earnings}, blackout through {last_blackout}")
        sizing = cfg["sizing"]
        check("position_slots", len(led["positions"]) < sizing["max_positions"],
              f"{len(led['positions'])} of {sizing['max_positions']} in use")
        check("not_already_held", item["symbol"] not in led["positions"])
        entered_today = sum(1 for p in led["positions"].values() if p["entry_date"] == today)
        check("daily_entry_cap", entered_today < cfg["execution"].get("max_new_positions_per_day", 99),
              f"{entered_today} entered today")
        # Budget is recomputed from what has actually filled so far this cycle.
        room = sleeve_equity(cfg, led) * sizing.get("max_exposure_pct", 1.0) - open_value(led)
        cash = led["broker"]["buying_power"] + queue.get("cash_delta", 0.0) - cfg["sleeve"].get("cash_buffer_dollars", 0.0)
        allowed = _floor_cents(min(dollars, room, cash))
        check("budget", allowed >= cfg["sleeve"].get("min_order_dollars", 1.0),
              f"planned {dollars:.2f}, sleeve room {room:.2f}, buying power {cash:.2f}")
        dollars = allowed
    else:
        pos = led["positions"].get(item["symbol"])
        check("position_exists", pos is not None and pos["qty"] + EPS >= item["qty"])
        if led["mode"] == "live":
            have = led["broker"]["positions"].get(item["symbol"], 0.0)
            check("broker_holds_shares", have + EPS >= item["qty"], f"broker shows {have}")

    blocking = [r for r in results if r["blocks"]]
    if blocking:
        item["status"] = "skipped"
        item["result"] = {"skipped_by": [r["check"] for r in blocking]}
        lg.journal("release_skip", {"mode": led["mode"], "order": item, "checks": results}, now)
        return {"ok": True, "action": "skip", "id": item_id, "symbol": item["symbol"],
                "failed_checks": blocking, "checks": results,
                "next_step": "Do NOT place this order. Move to the next order, or run `report` if none remain."}

    if buy:
        item["dollars"] = dollars
    order = _broker_order(item)
    if led["mode"] == "paper":
        price = ask if buy else bid
        qty = round(dollars / price, 6) if buy else item["qty"]
        _apply_fill(cfg, led, item, qty, price, "paper", now)
        return {"ok": True, "action": "paper_filled", "id": item_id, "symbol": item["symbol"],
                "side": item["side"], "qty": qty, "price": price, "checks": results,
                "next_step": "Paper mode: nothing was sent to the broker. Move to the next order, or run `report`."}

    item["status"] = "released"
    lg.journal("release", {"mode": led["mode"], "order": item, "checks": results}, now)
    return {"ok": True, "action": "place", "id": item_id, "order": order, "ref_id": item["ref_id"],
            "checks": results,
            "next_step": "Call place_equity_order with exactly these parameters plus account_number and "
                         "this ref_id. Then confirm with get_equity_orders and run "
                         f"`fill --id {item_id} --status filled --qty Q --price P --order-id ID` "
                         "(or --status rejected|cancelled|failed)."}


# --------------------------------------------------------------------------- fills

def fill(cfg: dict, led: dict, item_id: int, status: str, now: datetime, qty: float = 0.0,
         price: float = 0.0, order_id: str = "") -> dict:
    item = _find(led, item_id)
    if item is None:
        return {"ok": False, "error": f"no queued order with id {item_id}"}
    if item["status"] != "released":
        return {"ok": False, "error": f"order {item_id} is {item['status']}, not awaiting a fill"}
    if status == "filled":
        if qty <= 0 or price <= 0:
            return {"ok": False, "error": "a filled order needs --qty and --price from the broker's execution"}
        if item["side"] == "sell" and item["symbol"] not in led["positions"]:
            return {"ok": False, "error": f"{item['symbol']} is no longer in the ledger; nothing to book"}
        _apply_fill(cfg, led, item, qty, price, order_id, now)
    elif status in ("rejected", "cancelled", "failed"):
        item["status"] = status
        item["result"] = {"order_id": order_id}
        lg.journal("order_not_filled", {"mode": led["mode"], "order": item}, now)
    else:
        return {"ok": False, "error": "status must be filled, rejected, cancelled or failed"}
    left = [i["id"] for i in (led["queue"] or {}).get("items", []) if i["status"] == "pending"]
    return {"ok": True, "id": item_id, "status": item["status"], "remaining_order_ids": left,
            "next_step": (f"Next order: id {left[0]}." if left else "Queue complete. Run `report`.")}


def _apply_fill(cfg: dict, led: dict, item: dict, qty: float, price: float, order_id: str, now: datetime) -> None:
    today = now.date().isoformat()
    sym = item["symbol"]
    queue = led["queue"]
    if item["side"] == "buy":
        stop_dist = item["stop_dist_pct"]
        led["positions"][sym] = {
            "symbol": sym, "setup": item["setup"], "qty": qty, "avg_cost": price,
            "entry_date": today, "entry_ts": now.isoformat(timespec="seconds"),
            "stop": round(price * (1.0 - stop_dist), 4),
            "disaster_stop": round(disaster_stop_price(price, stop_dist, cfg["checks"]), 4),
            "stop_dist_pct": stop_dist, "order_id": order_id, "notes": item.get("notes", {}),
        }
        led["last_prices"][sym] = price
        queue["cash_delta"] = queue.get("cash_delta", 0.0) - qty * price
    else:
        pos = led["positions"][sym]
        sold = min(qty, pos["qty"])
        pnl = sold * (price - pos["avg_cost"])
        led["realized_pnl"] += pnl
        led["closed_trades"].append({
            "symbol": sym, "setup": pos["setup"], "entry_date": pos["entry_date"], "exit_date": today,
            "entry": pos["avg_cost"], "exit": price, "qty": sold, "pnl": round(pnl, 4),
            "ret": round(price / pos["avg_cost"] - 1.0, 5), "reason": item["reason"],
            "bars_held": item.get("bars_held"), "order_id": order_id,
        })
        if pos["qty"] - sold <= EPS:
            del led["positions"][sym]
            led["last_prices"].pop(sym, None)
        else:
            pos["qty"] -= sold
        queue["cash_delta"] = queue.get("cash_delta", 0.0) + sold * price
        if pnl < 0:
            led["consecutive_losses"] += 1
            limits = cfg["loss_limits"]
            if led["consecutive_losses"] >= limits.get("consecutive_losses_for_cooldown", 99):
                until = add_trading_days(now.date(), limits.get("cooldown_trading_days", 1), cfg)
                led["cooldown_until"] = until.isoformat()
                led["consecutive_losses"] = 0
        else:
            led["consecutive_losses"] = 0
        if "stop" in item["reason"]:
            days = cfg.get("locks", {}).get("after_stop_days", 3)
            led["locks"][sym] = add_trading_days(now.date(), days, cfg).isoformat()
    item["status"] = "filled"
    item["result"] = {"qty": qty, "price": price, "order_id": order_id}
    lg.journal("fill", {"mode": led["mode"], "order": item}, now)


# --------------------------------------------------------------------------- report

def report(cfg: dict, led: dict, now: datetime, note: Optional[str] = None) -> dict:
    today = now.date().isoformat()
    queue = led.get("queue") or {"items": [], "session": None, "date": None}
    unresolved = [i["id"] for i in queue["items"] if i["status"] in ("pending", "released")]
    pnl = total_pnl(led)
    eq = sleeve_equity(cfg, led)
    mark = {"date": today, "total_pnl": round(pnl, 4), "sleeve_equity": round(eq, 2)}
    if led["marks"] and led["marks"][-1]["date"] == today:
        led["marks"][-1] = mark
    else:
        led["marks"].append(mark)
    led["peak_pnl"] = max(led.get("peak_pnl", 0.0), pnl)
    if queue.get("session") == "close" and queue.get("date") == today and not unresolved \
            and is_trading_day(now.date(), cfg) and today not in led["cycle_dates"]:
        led["cycle_dates"].append(today)
    led["locks"] = {s: d for s, d in led["locks"].items() if d >= today}
    breakers = evaluate_breakers(cfg, led, today)
    summary = {
        "ok": True, "mode": led["mode"], "date": today, "session": queue.get("session"),
        "sleeve_equity": round(eq, 2), "deployed": round(open_value(led), 2),
        "open_positions": len(led["positions"]),
        "realized_pnl": round(led["realized_pnl"], 2), "unrealized_pnl": round(unrealized_pnl(led), 2),
        "total_pnl": round(pnl, 2), "closed_trades": len(led["closed_trades"]),
        "orders_this_cycle": {s: sum(1 for i in queue["items"] if i["status"] == s)
                              for s in ("filled", "skipped", "rejected", "cancelled", "failed", "pending", "released")},
        "unresolved_order_ids": unresolved, "breakers": breakers,
        "paper_cycle_days": len(set(lg.load("paper")["cycle_dates"])) if led["mode"] == "live" else len(set(led["cycle_dates"])),
        "next_step": "Commit and push the state/ directory.",
    }
    if note:
        summary["mode_note"] = note
    write_status(cfg, led, now, summary)
    lg.journal("report", {k: v for k, v in summary.items() if k != "next_step"}, now)
    return summary


def write_status(cfg: dict, led: dict, now: datetime, summary: dict) -> None:
    lines = [
        "# BlitzTrader status",
        "",
        f"_Updated {now.strftime('%Y-%m-%d %H:%M ET')} · mode **{led['mode']}** · last session: {summary.get('session')}_",
        "",
        f"- Sleeve size ({cfg['sleeve']['allocation_pct']:.0%} of the account): **${summary['sleeve_equity']:.2f}**",
        f"- Deployed: ${summary['deployed']:.2f} in {summary['open_positions']} position(s)",
        f"- P&L since start: realized ${summary['realized_pnl']:+.2f}, open ${summary['unrealized_pnl']:+.2f}, "
        f"total **${summary['total_pnl']:+.2f}**",
        f"- Closed trades: {summary['closed_trades']}",
        f"- New entries: {'BLOCKED - ' + '; '.join(summary['breakers']['reasons']) if summary['breakers']['block_entries'] else 'allowed'}",
        "",
        "## Open positions",
        "",
    ]
    if led["positions"]:
        lines += ["| Symbol | Setup | Entered | Qty | Cost | Last | P&L | Stop | Disaster stop |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for sym in sorted(led["positions"]):
            p = led["positions"][sym]
            last = _price(led, sym)
            lines.append(f"| {sym} | {p['setup']} | {p['entry_date']} | {p['qty']:.6f} | {p['avg_cost']:.2f} | "
                         f"{last:.2f} | {p['qty'] * (last - p['avg_cost']):+.2f} | {p['stop']:.2f} | {p['disaster_stop']:.2f} |")
    else:
        lines.append("None.")
    lines += ["", "## Last 15 closed trades", ""]
    if led["closed_trades"]:
        lines += ["| Symbol | Setup | In | Out | Days | Return | P&L | Exit reason |", "|---|---|---|---|---|---|---|---|"]
        for t in led["closed_trades"][-15:][::-1]:
            ret = f"{t['ret']:+.2%}" if t.get("ret") is not None else "n/a"
            lines.append(f"| {t['symbol']} | {t['setup']} | {t['entry_date']} | {t['exit_date']} | "
                         f"{t.get('bars_held', '')} | {ret} | {t['pnl']:+.2f} | {t['reason']} |")
    else:
        lines.append("None yet.")
    if led["incidents"]:
        lines += ["", "## Incidents", ""]
        lines += [f"- {i['date']}: {i['type']} {i['symbol']} (ledger {i['ledger_qty']}, broker {i['broker_qty']})"
                  for i in led["incidents"][-10:]]
    os.makedirs(lg.state_dir(), exist_ok=True)
    name = "STATUS.md" if led["mode"] == "live" else "STATUS.paper.md"
    with open(os.path.join(lg.state_dir(), name), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
