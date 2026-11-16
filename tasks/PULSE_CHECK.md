# Pulse check

The check between trades. Scheduled twice each weekday: shortly after the open (about 9:47 am
Eastern) and at midday (about 12:41 pm Eastern). It never opens a position. It does two things:

- sells any position that has fallen through its emergency ("disaster") stop, and
- reconciles the bot's ledger against the broker, so drift is caught within hours.

The hard rules in `tasks/CLOSE_RUN.md` apply here unchanged. Read them first.

## Steps

### 1. Is there anything to check?

```bash
git pull --rebase
python3 -m blitz status
```

If `positions` is empty, stop here: there is nothing to protect. Do not call any broker tool,
do not commit. Say "no open positions" and finish.

### 2. Broker snapshot

As in the close run: `get_accounts` (the one tradable account, ACCOUNT), `get_portfolio`,
`get_equity_positions`.

```bash
python3 -m blitz sync --total-value <total_value> --buying-power <buying_power> \
  --position SYM=QTY --position SYM=QTY ...
```

### 3. Plan

`get_equity_quotes` for every symbol in `sleeve_positions`, then:

```bash
python3 -m blitz plan --session pulse --quote SYM=bid:ask:last --quote SYM=bid:ask:last ...
```

A quote is required for every sleeve position. A pulse plan only ever contains sells.

### 4. Work the queue

Exactly as step 4 of the close run (quote, `review_equity_order`, `release`, then `place` and
`fill` in live mode). There is no earnings lookup for sells.

### 5. Report and save

```bash
python3 -m blitz report
git add state && git commit -m "pulse $(date +%F-%H%M)" && git push
```

Finish with one or two lines: positions checked, anything sold and why, any incidents.
