# BlitzTrader

An aggressive, short-hold swing trading bot for Robinhood, run by scheduled Claude sessions
through the Robinhood MCP. It trades a fixed 25% slice of one account, holds positions for one
to five days (about three on average), and checks itself between every trade.

It is a rule-based sibling of [FriesTrader](https://github.com/YizhiSong/FriesTrader): same
"agent as the hands, repository as the memory" shape, but faster, and with no judgment calls
left to the language model.

**Status: paper mode.** It reads the real account and simulates orders until you switch it on.

> Not financial advice. This places real orders with real money when live. The backtest shows a
> small edge that may not exist in the future. Only fund it with money you can afford to lose.

## What it does

Each weekday, about 20 minutes before the close, it scans 189 liquid stocks and ETFs for two
setups:

- **Dip:** a volatile stock that is in an uptrend and among the strongest of the last six
  months has just had a one- or two-day washout. Buy it; sell into the bounce.
- **Breakout:** a volatile stock closes at a new 20-day high on heavy volume. Buy it; hold
  while it keeps making higher lows, three days at most.

It holds up to five positions of equal size and sells on a snap-back, a failed trend, a stop,
a time limit, or ahead of earnings. Twice more each day a "pulse" session checks for emergency
exits and reconciles its books with the broker. Full rules: [docs/STRATEGY.md](docs/STRATEGY.md).

## How a session works

```
scheduled session (Claude + Robinhood MCP)          python3 -m blitz  (this repo)
--------------------------------------------        ------------------------------------------
read account value, cash, positions        ---->    sync     reconcile with the bot's own ledger
read quotes / earnings for held positions  ---->    plan     decide exits, then entries
for each order, one at a time:
  fresh quote + Robinhood order review     ---->    release  the between-trade checks
  place the order (live mode only)         <----             "place", "skip" or "paper_filled"
  confirm the fill                         ---->    fill     book it; unlock the next order
                                           ---->    report   mark P&L, breakers, STATUS.md
commit and push state/
```

The Python makes every decision and never talks to the broker. The agent talks to the broker
and makes no decisions.

## Checks between trades

Every order must pass `release` immediately before it is sent: market open and in its time
window, sane fresh quote, no alert from Robinhood's own order review, and the previous order
resolved. Buys must also match the planned price within 2%, have a spread of 0.2% or less, be
clear of earnings for three days, fit the sleeve budget and the account's buying power as they
stand after the previous fills, and not be blocked by a circuit breaker. A failed check skips
that order and is written to the journal.

Circuit breakers stop new buying after a 5% daily or 10% weekly loss on the sleeve, a 25%
drawdown, or four losses in a row. Exits are never blocked.

## What to expect

Backtested on 2016 to 2026 daily data ([docs/RESEARCH.md](docs/RESEARCH.md)):

| | Return / yr | Worst drawdown | Trades / yr | Win rate | Avg hold |
|---|---|---|---|---|---|
| BlitzTrader rules | +15.5% | 29% | 268 | 60% | 2.9 days |
| ... on symbols it was never tuned on | +4.0% | 51% | 210 | 59% | 2.9 days |
| ... if trading costs are 0.2% per side | -0.6% | 53% | 268 | 58% | 2.9 days |
| SPY, buy and hold | +12.8% | 34% | | | |
| QQQ, buy and hold | +18.1% | 36% | | | |

In plain terms: it is aggressive in how often it trades, not in what it is likely to earn. The
honest expectation is something like the market's return with a different shape, and a real
chance of doing worse. It is not a shortcut to large gains.

## Quick start

```bash
python3 -m unittest discover -s tests -t .
```

```bash
python3 -m blitz.backtest
```

```bash
python3 -m blitz golive-check
```

Then schedule the three sessions and paper trade for a week: [docs/SETUP.md](docs/SETUP.md).

## Repository layout

| Path | What it is |
|---|---|
| `risk_rules.json` | Every tunable number: sleeve size, universe, setups, checks, loss limits, mode |
| `tasks/CLOSE_RUN.md`, `tasks/PULSE_CHECK.md` | The step-by-step playbooks the scheduled sessions follow |
| `blitz/strategies.py` | Entry setups and exit rules, shared by backtest and live |
| `blitz/engine.py` | sync, plan, release (between-trade checks), fill, report |
| `blitz/backtest.py` | Portfolio backtest |
| `blitz/data.py`, `indicators.py`, `sizing.py`, `clock.py`, `ledger.py`, `cli.py` | Supporting modules |
| `state/` | The bot's ledger, journal and status page (written by the sessions) |
| `tests/` | 50 tests of the signals and the safety paths |
| `docs/` | Strategy, research, setup |

## Known limits

- Fractional positions cannot have resting stop orders at Robinhood, so stops are enforced by
  the scheduled sessions. A gap between sessions is not protected.
- Daily price bars come from Yahoo's public chart endpoint. If it is unreachable there are no
  new entries that day. Every order is cross-checked against a Robinhood quote before it goes.
- The account is shared with another bot. Read "Sharing the account" in
  [docs/SETUP.md](docs/SETUP.md) before going live.
- Short-term trading is taxed as ordinary income and produces wash sales.
