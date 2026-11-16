# state/

The bot's memory. Scheduled sessions write here and commit it; do not edit by hand.

- `ledger.paper.json`, `ledger.live.json`: positions, closed trades, P&L marks, locks, the current order queue.
- `journal.jsonl`: append-only audit trail of every sync, plan, check, skip and fill.
- `STATUS.paper.md`, `STATUS.md`: human-readable snapshot, rewritten each session.
- `HALT`: present only while a manual halt is in force (`python3 -m blitz halt`).
