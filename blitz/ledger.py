"""The bot's own book: which positions are its own, what it has made and lost, what is queued.

The brokerage account is shared with other strategies, so the broker's position list is not the
truth about this bot. The ledger is. One file per mode keeps paper trades out of the live book.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def state_dir() -> str:
    return os.environ.get("BLITZ_STATE_DIR") or os.path.join(ROOT, "state")


def ledger_path(mode: str) -> str:
    return os.path.join(state_dir(), f"ledger.{mode}.json")


def new_ledger(mode: str) -> dict:
    return {
        "version": 1,
        "mode": mode,
        "positions": {},          # symbol -> position dict
        "closed_trades": [],
        "realized_pnl": 0.0,
        "locks": {},              # symbol -> last locked date (YYYY-MM-DD), inclusive
        "marks": [],              # one per day: {date, total_pnl, sleeve_equity}
        "peak_pnl": 0.0,
        "cycle_dates": [],        # dates with a completed close cycle
        "consecutive_losses": 0,
        "cooldown_until": None,
        "last_prices": {},
        "broker": None,           # last sync snapshot
        "queue": None,            # current cycle's orders
        "incidents": [],
    }


def load(mode: str) -> dict:
    path = ledger_path(mode)
    if not os.path.exists(path):
        return new_ledger(mode)
    with open(path, "r", encoding="utf-8") as fh:
        ledger = json.load(fh)
    for key, value in new_ledger(mode).items():
        ledger.setdefault(key, value)
    return ledger


def save(ledger: dict) -> None:
    os.makedirs(state_dir(), exist_ok=True)
    path = ledger_path(ledger["mode"])
    fd, tmp = tempfile.mkstemp(dir=state_dir(), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def journal(event: str, payload: dict, now: Optional[datetime] = None) -> None:
    """Append one line to the append-only audit trail."""
    os.makedirs(state_dir(), exist_ok=True)
    line = {"ts": (now or datetime.now()).isoformat(timespec="seconds"), "event": event}
    line.update(payload)
    with open(os.path.join(state_dir(), "journal.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")


def halt_path() -> str:
    return os.path.join(state_dir(), "HALT")


def is_halted() -> bool:
    return os.path.exists(halt_path())
