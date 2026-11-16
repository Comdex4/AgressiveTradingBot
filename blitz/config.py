"""Load risk_rules.json, the single source of every tunable number."""

from __future__ import annotations

import json
import os
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(ROOT, "risk_rules.json")

REQUIRED = ["execution", "sleeve", "universe", "setups", "sizing", "checks", "loss_limits"]


def load_config(path: Optional[str] = None) -> dict:
    with open(path or DEFAULT_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    missing = [k for k in REQUIRED if k not in cfg]
    if missing:
        raise ValueError(f"risk_rules.json is missing sections: {missing}")
    if cfg["execution"].get("mode") not in ("paper", "live"):
        raise ValueError("execution.mode must be 'paper' or 'live'")
    alloc = cfg["sleeve"].get("allocation_pct", 0)
    if not 0 < alloc <= 1:
        raise ValueError("sleeve.allocation_pct must be in (0, 1]")
    return cfg
