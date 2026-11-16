# Strategy and plan

BlitzTrader is a short-hold swing bot. It holds stocks for one to five trading days (2.9 on
average in testing, three quarters of trades three days or less), is always long, and trades a
fixed slice of one Robinhood account. This page is the full rulebook. Every number on it lives
in `risk_rules.json`.

## The idea in one paragraph

Volatile stocks that are in an uptrend and among the strongest of the last six months tend to
snap back after a one- or two-day washout. The bot buys that washout a few minutes before the
close and sells into the bounce. Separately, when a volatile stock closes at a new 20-day high
on heavy volume, the move tends to carry for a few more days, and the bot rides it until it
stops making higher lows. Both trades are small, frequent and short. The two setups complement
each other: dips appear in pullbacks, breakouts in rallies.

## What it trades

- A fixed list of 189 liquid US stocks and ETFs (`universe.symbols`), including a few 3x
  leveraged index ETFs. All must trade above $5 with at least $20M average daily dollar volume.
- Long only. Whole or fractional shares, bought with dollar-based market orders. No options,
  no shorting, no margin, no crypto.
- Symbols held in the account by anything other than this bot are excluded, so the bot never
  mixes its shares with another strategy's.

## How much it trades

- **Sleeve:** 25% of the account's total value (`sleeve.allocation_pct`), recomputed every
  session. Deposit more and the sleeve grows with it; nothing else needs changing.
- **Positions:** up to 5 at a time, each 20% of the sleeve. The sleeve can be fully invested.
- **In a bear market** (SPY below its 200-day average): dip positions are half size and
  breakouts are not taken.

## Entries (judged once a day, about 20 minutes before the close)

### Dip ("washout in a leader")

All of:

1. Close above the 200-day average (it is in an uptrend).
2. RSI(2) at or below 10 (it has been hit hard for a day or two).
3. Average true range at least 2.5% of price (it moves enough for a bounce to matter).
4. Six-month return in the top 30% of the universe (it is a leader, not a faller).

Candidates are ranked by six-month return, strongest first.

### Breakout

All of:

1. Close above the highest high of the prior 20 days.
2. Volume at least 1.5 times the 20-day average.
3. Close in the upper half of the day's range.
4. Close above the 50-day average, and the 50-day above the 200-day.
5. No more than 15% above the 20-day average (not already stretched).
6. Average true range at least 2.5% of price.
7. Market in a bull regime.

Breakouts are rarer and are filled first; dips take the remaining slots.

## Exits

| Rule | Dip | Breakout | When it is checked |
|---|---|---|---|
| Snap-back: close above the 5-day average, or above yesterday's high | yes | | close run |
| Trend failure: close below yesterday's low | | yes | close run |
| Time stop | 5 trading days | 3 trading days | close run |
| Stop: 3x ATR below entry (dip, 3% to 15%) or 2x ATR (breakout, 2% to 12%) | yes | yes | close run |
| Disaster stop: 1.6x the stop distance, at most 22% below entry | yes | yes | every session |
| Earnings: report due today or the next trading day | yes | yes | close run |

A position is never sold on the day it was bought, so the bot makes no day trades.

**Why the normal stop is only checked at the close.** In testing, stops that fire intraday cut
returns by about a quarter: this kind of trade is bought into weakness, often dips further
during the next session, and recovers by the close. So the ordinary stop is a closing stop, and
a wider disaster stop is the one that is live during the day. Because the positions are
fractional shares, Robinhood cannot hold a resting stop order for them; the pulse checks are
what enforce the disaster stop.

## Checks between trades

No order reaches the broker without passing `release`, which runs before every single order and
refuses to release the next one until the previous one has been resolved.

For every order:

- It is the next one in the queue, and the previous one is recorded as filled or dead.
- The market is open and the order is inside its time window (entries 3:20 to 3:58 pm ET).
- A fresh broker quote is sane.
- Robinhood's own pre-trade review (`review_equity_order`) raised no alert.

For buys, additionally:

- The broker's price is within 2% of the price the plan was built on. This cross-checks the
  public price feed against the broker before money moves.
- The bid/ask spread is at most 0.2%. The edge is small; a wide spread eats it.
- No earnings report within the next 3 trading days.
- A position slot is free, the symbol is not already held, and the daily entry cap is not hit.
- The sleeve has room and the account has buying power, recomputed from what actually filled
  so far in this session. The order is shrunk to fit, or skipped if under $1.
- No circuit breaker is tripped and no manual halt is set.

Sells skip the price and spread blocks: getting out matters more than the fill.

## Circuit breakers

These block new entries only. Exits always run.

| Trigger | Effect |
|---|---|
| Sleeve down 5% on the day | no entries today |
| Sleeve down 10% over five trading days | no entries until it recovers |
| Sleeve 15% below its high-water mark | new positions half size |
| Sleeve 25% below its high-water mark | no entries until a human runs `reset-drawdown` |
| 4 losing trades in a row | no entries for one trading day |
| A stop was hit on a symbol | that symbol is locked for 3 trading days |
| `python3 -m blitz halt` | no entries until `resume` |

## A day in the life

| Time (ET) | Session | What happens |
|---|---|---|
| ~9:47 am | pulse | Reconcile ledger with broker. Sell anything through its disaster stop. |
| ~12:41 pm | pulse | Same. |
| ~3:28 pm | close run | Reconcile. Sell what should be sold. Scan the universe. Buy up to the free slots. Write `state/STATUS.md`. |

Each session is a fresh agent session that pulls this repository, runs the engine, and pushes
the `state/` directory back. The repository is the bot's memory.

## Paper first

`execution.mode` starts as `paper`. In paper mode the bot does everything except send orders:
it reads the real account, gets real quotes, runs Robinhood's order simulation, and books
simulated fills at the bid or ask in a separate paper ledger. Live mode is refused until 5
completed paper close runs are on record, and then only after a human changes the mode.

## What this design deliberately leaves out

- **Judgment by the language model.** The model never picks a stock or a size. That makes the
  bot testable and removes the risk of it being talked into a trade by something it read.
- **News.** No news search or thesis. The only fundamental input is the earnings date.
- **Intraday trading.** No same-day round trips, so no pattern-day-trader exposure.
- **Averaging down, pyramiding, partial exits.** One entry, one exit, per position.
