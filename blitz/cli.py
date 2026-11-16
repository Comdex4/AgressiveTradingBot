"""Command line for the agent session. Every command prints exactly one JSON object.

    python -m blitz sync --total-value 110.27 --buying-power 29.33 --position BB=1.425062 ...
    python -m blitz plan --session close [--quote SYM=bid:ask:last ...] [--earnings SYM=YYYY-MM-DD ...]
    python -m blitz release --id 1 --quote bid:ask:last --review ok [--earnings YYYY-MM-DD|none|unknown]
    python -m blitz fill --id 1 --status filled --qty 0.1234 --price 45.67 --order-id <uuid>
    python -m blitz report
    python -m blitz status | halt | resume | reset-drawdown | golive-check
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional

from . import engine
from . import ledger as lg
from .clock import now_et
from .config import load_config
from .data import load_mcp_files


def _quote(text: str) -> dict:
    parts = text.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("quote must be bid:ask:last")
    bid, ask, last = (float(p) for p in parts)
    return {"bid": bid, "ask": ask, "last": last}


def _pairs(values: Optional[List[str]], what: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in values or []:
        if "=" not in raw:
            raise SystemExit(json.dumps({"ok": False, "error": f"{what} must look like SYMBOL=value, got {raw!r}"}))
        key, value = raw.split("=", 1)
        out[key.strip().upper()] = value.strip()
    return out


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="blitz", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="path to risk_rules.json")
    ap.add_argument("--now", default=None, help="override the clock (ISO, Eastern); for tests and replays")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sync", help="record the broker snapshot and reconcile the ledger")
    p.add_argument("--total-value", type=float, required=True, help="get_portfolio total_value")
    p.add_argument("--buying-power", type=float, required=True, help="get_portfolio buying_power.buying_power")
    p.add_argument("--position", action="append", help="every broker equity position, SYMBOL=QUANTITY (repeatable)")

    p = sub.add_parser("plan", help="decide this session's exits and entries")
    p.add_argument("--session", choices=["close", "pulse"], required=True)
    p.add_argument("--quote", action="append", help="broker quote for a sleeve position, SYMBOL=bid:ask:last")
    p.add_argument("--earnings", action="append", help="next earnings date of a sleeve position, SYMBOL=YYYY-MM-DD")
    p.add_argument("--mcp-bars", action="append",
                   help="fallback data: file holding a raw get_equity_historicals result (repeatable)")

    p = sub.add_parser("release", help="between-trade checks for one queued order")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--quote", type=_quote, required=True, help="fresh broker quote, bid:ask:last")
    p.add_argument("--review", choices=["ok", "alert"], required=True,
                   help="'ok' if review_equity_order returned no alert, else 'alert'")
    p.add_argument("--alert-text", default="", help="the alert from review_equity_order, if any")
    p.add_argument("--earnings", default="unknown", help="next earnings date YYYY-MM-DD, 'none' (ETF), or 'unknown'")

    p = sub.add_parser("fill", help="record the broker's outcome for a released order")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--status", choices=["filled", "rejected", "cancelled", "failed"], required=True)
    p.add_argument("--qty", type=float, default=0.0, help="cumulative_quantity from the broker")
    p.add_argument("--price", type=float, default=0.0, help="average_price from the broker")
    p.add_argument("--order-id", default="")

    sub.add_parser("report", help="mark the book and write the status file")
    sub.add_parser("status", help="show the ledger summary without changing anything")
    p = sub.add_parser("halt", help="block all new entries until `resume`")
    p.add_argument("--reason", default="manual")
    sub.add_parser("resume", help="remove the manual halt")
    sub.add_parser("reset-drawdown", help="human acknowledgement after a drawdown halt")
    sub.add_parser("golive-check", help="is the bot allowed to trade live yet?")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    now = now_et(args.now)
    mode, note = engine.effective_mode(cfg)
    led = lg.load(mode)
    save = True

    if args.cmd == "sync":
        positions = {k: float(v) for k, v in _pairs(args.position, "--position").items()}
        out = engine.sync(cfg, led, args.total_value, args.buying_power, positions, now)
    elif args.cmd == "plan":
        quotes = {k: _quote(v) for k, v in _pairs(args.quote, "--quote").items()}
        data = load_mcp_files(args.mcp_bars) if args.mcp_bars else None
        out = engine.plan(cfg, led, args.session, now, data=data, quotes=quotes,
                          earnings=_pairs(args.earnings, "--earnings"))
    elif args.cmd == "release":
        out = engine.release(cfg, led, args.id, args.quote, args.review, now,
                             earnings=args.earnings, alert_text=args.alert_text)
    elif args.cmd == "fill":
        out = engine.fill(cfg, led, args.id, args.status, now, qty=args.qty, price=args.price,
                          order_id=args.order_id)
    elif args.cmd == "report":
        out = engine.report(cfg, led, now, note)
    elif args.cmd == "status":
        save = False
        out = {"ok": True, "mode": mode, "positions": led["positions"],
               "realized_pnl": round(led["realized_pnl"], 2),
               "unrealized_pnl": round(engine.unrealized_pnl(led), 2),
               "closed_trades": len(led["closed_trades"]), "locks": led["locks"],
               "cooldown_until": led["cooldown_until"], "halted": lg.is_halted(),
               "queue": led.get("queue"), "cycle_days": sorted(set(led["cycle_dates"]))}
    elif args.cmd == "halt":
        save = False
        os.makedirs(lg.state_dir(), exist_ok=True)
        with open(lg.halt_path(), "w", encoding="utf-8") as fh:
            fh.write(f"{now.isoformat(timespec='seconds')} {args.reason}\n")
        lg.journal("halt", {"reason": args.reason}, now)
        out = {"ok": True, "halted": True, "note": "New entries are blocked. Exits still run."}
    elif args.cmd == "resume":
        save = False
        if lg.is_halted():
            os.remove(lg.halt_path())
        lg.journal("resume", {}, now)
        out = {"ok": True, "halted": False}
    elif args.cmd == "reset-drawdown":
        led["peak_pnl"] = engine.total_pnl(led)
        lg.journal("reset_drawdown", {"peak_pnl": led["peak_pnl"]}, now)
        out = {"ok": True, "peak_pnl": round(led["peak_pnl"], 2)}
    else:  # golive-check
        save = False
        need = cfg["execution"].get("paper_min_cycle_days_before_live", 5)
        have = sorted(set(lg.load("paper")["cycle_dates"]))
        out = {"ok": True, "configured_mode": cfg["execution"]["mode"], "effective_mode": mode,
               "paper_cycle_days": have, "required": need, "ready_for_live": len(have) >= need,
               "how": "When ready, a human sets execution.mode to \"live\" in risk_rules.json and commits it."}

    if note and "mode_note" not in out:
        out["mode_note"] = note
    if save:
        lg.save(led)
    print(json.dumps(out, indent=2, sort_keys=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
