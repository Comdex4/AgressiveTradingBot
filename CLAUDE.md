# BlitzTrader: notes for agent sessions

This repository is a trading bot that places real orders through the Robinhood MCP when it is in
live mode. Read this before doing anything here.

## If you were started by a schedule

Your prompt names a task file. Follow `tasks/CLOSE_RUN.md` or `tasks/PULSE_CHECK.md` exactly,
including its hard rules. Do not improvise, and do not do development work in a trading session.

## If you are doing development work

- Never place, review-then-place, or cancel a broker order as part of development. Read-only
  broker calls and `review_equity_order` (a simulation) are fine for testing.
- Point experiments at a scratch state directory so the real ledger is untouched:
  `export BLITZ_STATE_DIR=/some/scratch/dir`. Use `--now 2026-11-16T15:40:00` to simulate a time.
- Run the tests before committing: `python3 -m unittest discover -s tests -t .`
- The engine is stdlib-only Python on purpose. Do not add dependencies.
- Every tunable number lives in `risk_rules.json`. Changing strategy numbers there without
  re-running `python3 -m blitz.backtest` is how money gets lost: run it and compare with
  `docs/RESEARCH.md`.
- `execution.mode` in `risk_rules.json` is changed by the human owner only.
- Do not commit the brokerage account number anywhere.

## Layout

- `blitz/strategies.py`: entry setups and exit rules (shared by backtest and live).
- `blitz/engine.py`: sync, plan, release (between-trade checks), fill, report.
- `blitz/backtest.py`: portfolio backtest on daily bars.
- `state/`: the bot's ledger, journal and status page. This is the bot's memory.
- `docs/`: the strategy, the research behind it, and setup.
