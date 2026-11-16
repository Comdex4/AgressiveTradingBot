import unittest
from datetime import date

from blitz import indicators as ta
from blitz.backtest import run_backtest
from blitz.clock import add_trading_days, trading_days_between, window_status
from blitz.config import load_config
from blitz.data import parse_mcp_historicals, parse_yahoo
from blitz.strategies import Ind, breakout_signal, dip_signal, exit_reason, momentum_ranks

from tests.helpers import StateCase, at, breakout_series, dip_series, make_series


class IndicatorTests(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(ta.sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])

    def test_rsi_extremes(self):
        up = ta.rsi([1, 2, 3, 4, 5, 6], 2)
        self.assertEqual(up[-1], 100.0)
        down = ta.rsi([6, 5, 4, 3, 2, 1], 2)
        self.assertEqual(down[-1], 0.0)
        self.assertIsNone(up[1])

    def test_rsi_uses_no_future_data(self):
        closes = [10, 11, 10.5, 11.2, 10.8, 11.5, 11.1, 12.0]
        full = ta.rsi(closes, 2)
        for i in range(3, len(closes)):
            self.assertAlmostEqual(ta.rsi(closes[: i + 1], 2)[-1], full[i])

    def test_atr_and_ibs(self):
        h, l, c = [10, 11, 12], [9, 10, 11], [9.5, 10.5, 11.5]
        self.assertAlmostEqual(ta.atr(h, l, c, 2)[-1], 1.375)
        self.assertEqual(ta.ibs([10], [8], [9.5]), [0.75])
        self.assertEqual(ta.ibs([10], [10], [10]), [0.5])

    def test_prior_windows_exclude_today(self):
        self.assertEqual(ta.prior_max([1, 5, 2, 9], 2), [None, None, 5, 5])
        self.assertEqual(ta.prior_mean([2, 4, 6, 100], 2), [None, None, 3.0, 5.0])


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.common = self.cfg["common"]

    def test_dip_fires_on_washout_in_uptrend(self):
        ind = Ind(dip_series())
        sig = dip_signal(ind, len(ind.s) - 1, self.cfg["setups"]["dip"], self.common)
        self.assertIsNotNone(sig)
        self.assertLess(sig.stop, sig.price)
        self.assertLessEqual(sig.stop_dist_pct, self.cfg["setups"]["dip"]["stop_max_pct"] + 1e-9)

    def test_dip_silent_without_washout_or_trend(self):
        calm = Ind(make_series("CALM"))
        self.assertIsNone(dip_signal(calm, len(calm.s) - 1, self.cfg["setups"]["dip"], self.common))
        downtrend = Ind(make_series("DOWN", drift=-0.002, tail=(-0.04, -0.05)))
        self.assertIsNone(dip_signal(downtrend, len(downtrend.s) - 1, self.cfg["setups"]["dip"], self.common))

    def test_dip_needs_volatility_and_liquidity(self):
        quiet = Ind(dip_series(day_range=0.005))
        self.assertIsNone(dip_signal(quiet, len(quiet.s) - 1, self.cfg["setups"]["dip"], self.common))
        thin = Ind(dip_series(volume=1000))
        self.assertIsNone(dip_signal(thin, len(thin.s) - 1, self.cfg["setups"]["dip"], self.common))

    def test_breakout_fires_on_high_volume_new_high(self):
        ind = Ind(breakout_series())
        sig = breakout_signal(ind, len(ind.s) - 1, self.cfg["setups"]["breakout"], self.common)
        self.assertIsNotNone(sig)
        quiet = breakout_series()
        quiet.volume[-1] = quiet.volume[-2]
        q = Ind(quiet)
        self.assertIsNone(breakout_signal(q, len(q.s) - 1, self.cfg["setups"]["breakout"], self.common))

    def test_exit_rules(self):
        p = self.cfg["setups"]["dip"]
        bounce = Ind(make_series("X", tail=(-0.04, -0.05, 0.08)))
        i = len(bounce.s) - 1
        self.assertIn(exit_reason("dip", bounce, i, bounce.s.close[i - 1], 1, p),
                      ("snapback_above_sma", "close_above_prior_high"))
        still_down = Ind(make_series("Y", tail=(-0.04, -0.05, -0.01)))
        j = len(still_down.s) - 1
        self.assertIsNone(exit_reason("dip", still_down, j, still_down.s.close[j - 1], 1, p))
        self.assertEqual(exit_reason("dip", still_down, j, still_down.s.close[j - 1], p["max_hold_days"], p), "time_stop")
        self.assertIsNone(exit_reason("dip", bounce, i, 1.0, 0, p), "never exit on the entry bar")
        bo = self.cfg["setups"]["breakout"]
        failed = Ind(make_series("Z", drift=0.001, tail=(0.06, -0.08)))
        self.assertEqual(exit_reason("breakout", failed, len(failed.s) - 1, 1.0, 1, bo), "close_below_prior_low")

    def test_momentum_ranks(self):
        inds = {"A": Ind(make_series("A", drift=0.004)), "B": Ind(make_series("B", drift=0.001)),
                "C": Ind(make_series("C", drift=-0.001))}
        ranks = momentum_ranks(inds, inds["A"].s.dates[-1])
        self.assertEqual(ranks, {"C": 0.0, "B": 0.5, "A": 1.0})


class ClockTests(StateCase):
    def test_trading_day_math(self):
        self.assertEqual(add_trading_days(date(2026, 11, 20), 1, self.cfg), date(2026, 11, 23))
        self.assertEqual(add_trading_days(date(2026, 11, 25), 1, self.cfg), date(2026, 11, 27), "skips Thanksgiving")
        self.assertEqual(trading_days_between(date(2026, 11, 12), date(2026, 11, 16), self.cfg), 2)

    def test_windows(self):
        self.assertTrue(window_status(at("15:40"), self.cfg, entry=True)[0])
        self.assertFalse(window_status(at("12:00"), self.cfg, entry=True)[0])
        self.assertTrue(window_status(at("12:00"), self.cfg, entry=False)[0])
        self.assertFalse(window_status(at("15:59"), self.cfg, entry=False)[0])
        self.assertFalse(window_status(at("15:40", date(2026, 11, 14)), self.cfg, entry=False)[0], "Saturday")
        self.assertFalse(window_status(at("15:40", date(2026, 11, 26)), self.cfg, entry=False)[0], "holiday")
        self.assertFalse(window_status(at("15:40", date(2026, 11, 27)), self.cfg, entry=False)[0], "half day")
        self.assertTrue(window_status(at("12:30", date(2026, 11, 27)), self.cfg, entry=False)[0])
        self.assertFalse(window_status(at("12:30", date(2026, 11, 27)), self.cfg, entry=True)[0])


class DataTests(unittest.TestCase):
    def test_parse_mcp_historicals(self):
        payload = {"data": {"results": [{"symbol": "SPY", "bars": [
            {"begins_at": "2026-11-12T00:00:00Z", "open_price": "753.98", "close_price": "757.12",
             "high_price": "763.04", "low_price": "753.67", "volume": 37460594},
            {"begins_at": "2026-11-13T00:00:00Z", "open_price": "1", "close_price": "1",
             "high_price": "1", "low_price": "1", "volume": 0, "interpolated": True}]}]}}
        s = parse_mcp_historicals(payload)["SPY"]
        self.assertEqual(s.dates, ["2026-11-12"])
        self.assertEqual(s.close, [757.12])

    def test_parse_yahoo_skips_empty_bars(self):
        payload = {"chart": {"result": [{"meta": {"gmtoffset": -18000}, "timestamp": [1794321000, 1794407400],
                   "indicators": {"quote": [{"open": [1.0, None], "high": [2.0, None], "low": [0.5, None],
                                             "close": [1.5, None], "volume": [10, None]}]}}]}}
        self.assertEqual(len(parse_yahoo("X", payload)), 1)

    def test_with_last_price(self):
        s = make_series("A", n=5)
        same_day = s.with_last_price(s.dates[-1], s.close[-1] * 2)
        self.assertEqual(len(same_day), 5)
        self.assertEqual(same_day.high[-1], s.close[-1] * 2)
        self.assertEqual(len(s.with_last_price("2099-01-01", 1.0)), 6)
        self.assertEqual(len(s), 5, "original untouched")


class BacktestTests(StateCase):
    def test_runs_and_trades_the_dip(self):
        data = self.market()
        out = run_backtest(data, self.cfg, data["SPY"].dates[210], data["SPY"].dates[-1], slippage_bps=0)
        self.assertIn("cagr", out)
        calm_only = {"SPY": data["SPY"], "CALM": data["CALM"]}
        flat = run_backtest(calm_only, self.cfg, data["SPY"].dates[210], data["SPY"].dates[-1], universe=["CALM"])
        self.assertEqual(flat["trades"], 0)
        self.assertEqual(flat["total_return"], 0.0, "no signal, no trade, capital untouched")

    def test_no_lookahead(self):
        """Dropping every bar after the test window must not change results inside it."""
        full = self.market()
        full["DIPP"] = make_series("DIPP", tail=(-0.04, -0.05, 0.03, 0.02, 0.01, -0.01, 0.01, 0.01))
        end = full["SPY"].dates[-5]
        cut = {k: v.truncated(end) for k, v in full.items()}
        a = run_backtest(full, self.cfg, full["SPY"].dates[210], end)
        b = run_backtest(cut, self.cfg, full["SPY"].dates[210], end)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
