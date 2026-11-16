# Setup and operation

## What you need

- A Robinhood account with agentic trading enabled (the account the Robinhood MCP can trade).
- Claude Code with the Robinhood connector attached, able to run scheduled sessions.
- This repository, which the scheduled sessions pull from and push `state/` back to.
- Network access from those sessions to `query1.finance.yahoo.com` (daily price bars).

Nothing to install: the engine is plain Python 3.10+ with no dependencies.

## 1. Check it locally

```bash
python3 -m unittest discover -s tests -t .
```

```bash
python3 -m blitz.backtest
```

```bash
python3 -m blitz golive-check
```

## 2. Schedule three sessions

Create three scheduled tasks that each start a **fresh session on this repository** with the
Robinhood connector attached. Times are US/Eastern, weekdays only.

| Task | Time (ET) | Prompt |
|---|---|---|
| Blitz close run | 3:28 pm | see below, with `tasks/CLOSE_RUN.md` |
| Blitz pulse (open) | 9:47 am | see below, with `tasks/PULSE_CHECK.md` |
| Blitz pulse (midday) | 12:41 pm | see below, with `tasks/PULSE_CHECK.md` |

Prompt for each (swap in the task file):

> You are running the BlitzTrader bot for me. Read `tasks/CLOSE_RUN.md` in this repository and
> carry it out exactly, including its hard rules. I authorise you to place the equity orders
> that `python3 -m blitz release` returns with action `place`, and only those, in my agentic
> Robinhood account, without asking me first. Do not ask me questions; if something is wrong,
> place nothing further, run the report, commit, and tell me what happened. Push the `state/`
> directory to the repository's default branch when you are done.

Notes:

- The close run must start on time. Entries are only allowed between 3:20 and 3:58 pm ET; a
  late start means exits only.
- The pulse checks cost almost nothing when the bot holds no positions (they stop at step 1).
- Do not let two schedulers run the same task.
- That paragraph is your standing instruction to trade without a per-order confirmation. If
  you would rather approve each order yourself, turn on trade approvals for the account in the
  Robinhood app; each order then waits for your tap, which only works if you are watching your
  phone at 3:40 pm.

## 3. Paper trade first

The bot ships in paper mode. Let the three sessions run for at least five trading days and read
`state/STATUS.paper.md` and `state/journal.jsonl`. Check that:

- the plans make sense to you (which names, how many, what size),
- skipped orders were skipped for good reasons,
- the sessions finish before 3:58 pm ET.

## 4. Go live

1. `python3 -m blitz golive-check` must say `ready_for_live: true`.
2. Sort out the shared-account issues below.
3. Edit `risk_rules.json`: set `execution.mode` to `"live"`. Commit and push. Only you do this.

The live book starts empty in `state/ledger.live.json`; paper positions are not carried over.

## Sharing the account with another bot

The account already runs another strategy. Three things follow from that.

1. **Cash.** Both bots spend the same buying power. If the other bot is allowed to use the
   whole account, it will sometimes leave this one with nothing, and the order is skipped
   (`budget` check). To reserve this bot's quarter, cap the other bot at 75% of the account,
   for example by raising its minimum cash buffer to 25%.
2. **The other bot can see, and may sell, this bot's positions.** A bot that manages "every
   position in the account" will apply its own stops and profit targets to shares this bot
   bought. BlitzTrader detects that at the next sync (an `external_reduction` incident in
   `state/STATUS.md`) and corrects its ledger, but the trade is disrupted. Fix it at the source:
   tell the other bot to ignore the symbols listed under `positions` in this repository's
   `state/ledger.live.json`, or give one of the bots its own agentic account.
3. **This bot never touches the other bot's positions**, and will not buy a symbol the other
   bot holds.

## Day-to-day controls

| Want to | Do |
|---|---|
| See where things stand | Open `state/STATUS.md` (or `STATUS.paper.md`) |
| Stop new buys now | `python3 -m blitz halt --reason "..."`, commit, push |
| Allow buys again | `python3 -m blitz resume`, commit, push |
| Go back to paper | Set `execution.mode` to `"paper"`, commit, push. Open live positions are then no longer managed: close them yourself in the app first. |
| Change the slice of the account | `sleeve.allocation_pct` in `risk_rules.json` |
| Clear a drawdown halt | `python3 -m blitz reset-drawdown`, commit, push |
| Audit a decision | `state/journal.jsonl` has every plan, check, skip and fill |

Stopping the bot completely means disabling the three scheduled tasks. Positions it holds stay
open until you sell them.

## Things that will go wrong eventually

- **A session dies after placing an order but before recording it.** The next session's `plan`
  refuses to continue until the order is looked up and recorded.
- **The price feed is down.** No entries that day; exits still run from broker quotes.
- **A session starts late or the market closes early.** Orders outside the window are refused.
  Update `calendar` in `risk_rules.json` each year with holidays and half days.
- **A stock gaps through the disaster stop overnight.** It is sold at the next pulse, at
  whatever the price then is. The stop limits how long a loss runs, not how large a gap is.
