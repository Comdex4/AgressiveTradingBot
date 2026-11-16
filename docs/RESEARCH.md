# Research: what was tested and what it showed

Everything here is a backtest on daily bars from 2016 through mid-November 2026, on real price
history (Yahoo's public chart data), run with `python3 -m blitz.backtest`. Backtests flatter.
Read the caveats before the numbers.

## Bottom line

- The shipped rules backtest at about **+15% a year with a 29% worst drawdown**, roughly 270
  trades a year, 60% winners, average gain +0.37% per trade, average hold 2.9 days.
- That is in the same range as simply holding SPY (+12.8% a year) or QQQ (+18.1%) over the same
  period. It is not a way to multiply money quickly. Its distinguishing feature is that it is
  invested only about 57% of the time.
- The edge per trade is small, so it is fragile: at 0.2% of trading cost per side it is gone.
- On a set of 111 symbols the rules were never tuned on, it made about +4% a year with a 51%
  drawdown. Treat that as a realistic bad case, not as a fluke.

## The shipped configuration

| Test | Return / yr | Worst drawdown | Sharpe | Trades / yr | Win rate | Avg trade |
|---|---|---|---|---|---|---|
| Full universe, 2016 to 2026 | +15.5% | 29.4% | 0.67 | 268 | 60% | +0.37% |
| First half only (2016 to 2021) | +16.8% | 25.8% | 0.75 | 244 | 60% | +0.41% |
| Second half only (2022 to 2026) | +13.9% | 27.3% | 0.59 | 299 | 59% | +0.33% |
| SPY buy and hold | +12.8% | 34.1% | 0.77 | | | |
| QQQ buy and hold | +18.1% | 35.6% | 0.86 | | | |

By year: 2016 +8%, 2017 +15%, 2018 -8%, 2019 +28%, 2020 +60%, 2021 +8%, 2022 -12%, 2023 +11%,
2024 +31%, 2025 +13%, 2026 (to mid-November) +30%.

Hold time: 2.9 trading days on average; 75% of trades closed within 3 days. Worst single trade
-31%. Exits: snap-back 55%, time stop 26%, breakout trend failure 14%, stop 3%, disaster
stop 1.4%.

By setup: dip 1,978 trades, 65% winners, +0.37% average. Breakout 934 trades, 48% winners,
+0.36% average.

Assumptions: 0.05% slippage per side, no commission, entries at the day's close, stop judged at
the close with the disaster stop live intraday, five positions of 20% each.

## Stress tests of the shipped configuration

| Change | Return / yr | Worst drawdown | Sharpe |
|---|---|---|---|
| None (baseline) | +15.5% | 29.4% | 0.67 |
| Slippage 0.10% per side | +9.8% | 36.5% | 0.49 |
| Slippage 0.20% per side | -0.6% | 52.5% | 0.11 |
| Enter at the next day's open instead of the close | +8.0% | 30.7% | 0.44 |
| Stops live intraday instead of at the close | +11.5% | 37.2% | 0.54 |
| No disaster stop | +16.8% | 31.4% | 0.72 |
| 3 positions of 34% | +14.7% | 44.1% | 0.58 |
| 4 positions of 25% | +15.2% | 31.9% | 0.64 |
| 6 positions of 17% | +15.5% | 29.6% | 0.71 |
| Only the 78 symbols the rules were first developed on | +18.2% | 32.5% | 0.89 |
| Only the 111 symbols added afterwards (never tuned on) | +4.0% | 51.0% | 0.29 |
| Full universe minus 15 obvious hindsight winners | +13.2% | 43.6% | 0.65 |

What these say:

- **Costs decide everything.** This is why buys are refused when the spread is above 0.2%.
- **Entering near the close matters.** Waiting for the next open halves the return, so the
  close run has to run on time.
- **Concentration hurts.** Three big positions give the same return with a far worse drawdown.
- **The disaster stop costs about a point a year.** It is kept as insurance against a collapse.

## Ideas explored

| Idea | Verdict | Evidence |
|---|---|---|
| Dip buying in uptrends (RSI(2) at or below 10) | Kept, with filters | Raw edge +0.2% to +0.4% per trade over about 4,000 trades in both halves; too thin unfiltered. |
| Only volatile names (ATR at least 2.5% to 3%) | Kept | Raw dip edge rises to +0.6% to +0.9% per trade. |
| Only leaders (top 30% by six-month return), ranked by it | Kept | The one selection rule that held up on the unseen symbols. |
| Rank dips by "most oversold" | Rejected | Loses money as a portfolio (-7% a year): it picks the worst falling knives. |
| Rank dips by "most volatile" | Rejected | Best on the original symbols, about zero on unseen ones. |
| Breakouts on volume in volatile names | Kept | +5% to +8% a year alone with a drawdown under 20%, while invested about a tenth of the time. |
| Gap-up continuation | Rejected | +0.1% per trade, negative when held longer. Left in the code, switched off. |
| Leveraged index ETFs only | Folded in | Good per trade (+0.5% to +1.5%) but too few trades alone. They are in the universe. |
| Hard 3-day time limit on dips | Rejected | Cuts the dip edge by about a third. Five days keeps the average near three. |
| Tight stops (2x ATR, 10%) | Rejected | Worse return and worse drawdown than wide stops. |
| Two or three concentrated positions | Rejected | Drawdowns of 45% to 80% in testing. |
| One new position per day (staggering) | Rejected | No clear benefit once five positions are used. |
| Block all entries in a bear market | Rejected | Slightly worse than half-size dips. |
| Add an index-product cap | Rejected | No consistent benefit. |

## Caveats

1. **Hindsight in the symbol list.** The universe was chosen in 2026. It includes names that
   are famous because they went up. The first version of these rules looked far better (+21% a
   year) on the first 78 symbols than on 111 others (about zero). The shipped rules were changed
   until they held up on both, but the list is still not a list anyone could have written in
   2016. Delisted stocks are missing entirely.
2. **Many variants were tried.** Roughly a hundred configurations were tested. Picking the best
   of many tests overstates the result. The first-half / second-half split and the unseen-symbol
   test are the guard against that; neither is perfect.
3. **Fills are assumed, not observed.** The test buys at the closing price. The live bot buys
   10 to 25 minutes earlier with a market order for a fractional share.
4. **Volume is partial at decision time.** The live breakout check sees most but not all of the
   day's volume, so it will take slightly fewer breakouts than the test.
5. **Earnings.** The test has no earnings filter; live trading avoids entries just before
   earnings and exits ahead of them. The effect of that difference is untested.
6. **Taxes.** Hundreds of short-term trades a year are taxed as ordinary income, and frequent
   re-buying after losses creates wash sales. None of that is in the numbers.
7. **Regime.** 2016 to 2026 was mostly a strong market for volatile growth stocks.

## Reproduce

```bash
python3 -m blitz.backtest                      # the shipped configuration
python3 -m blitz.backtest --slippage-bps 10    # stress it
python3 -m blitz.backtest --entry next_open
python3 -m blitz.backtest --start 2022-01-01
```

The first run downloads about twelve years of daily bars for 189 symbols into `data/cache/`.
Numbers will drift a little as new days are added.
