"""The safety-critical paths: reconciliation, exits, the between-trade checks, circuit breakers."""

import os
import unittest
from datetime import date

from blitz import engine
from blitz import ledger as lg

from tests.helpers import StateCase, at, make_series

GOOD_QUOTE = {"bid": 99.95, "ask": 100.05, "last": 100.0}


def quote_at(price: float) -> dict:
    return {"bid": round(price * 0.9995, 4), "ask": round(price * 1.0005, 4), "last": price}


class EngineCase(StateCase):
    def ledger(self, mode="paper"):
        led = lg.new_ledger(mode)
        engine.sync(self.cfg, led, 1000.0, 400.0, {"OTHER": 3.0}, at("15:30"))
        return led

    def hold(self, led, symbol="HELD", cost=100.0, qty=0.5, entry="2026-11-12", setup="dip"):
        led["positions"][symbol] = {
            "symbol": symbol, "setup": setup, "qty": qty, "avg_cost": cost, "entry_date": entry,
            "stop": cost * 0.90, "disaster_stop": cost * 0.84, "stop_dist_pct": 0.10, "order_id": "x"}
        led["last_prices"][symbol] = cost
        led["broker"]["positions"][symbol] = qty

    def planned(self, led, **kw):
        return engine.plan(self.cfg, led, "close", at("15:40"), data=self.market(), **kw)


class SyncTests(EngineCase):
    def test_sleeve_is_a_quarter_of_the_account(self):
        led = self.ledger()
        self.assertEqual(engine.sleeve_equity(self.cfg, led), 250.0)

    def test_other_strategies_holdings_are_foreign(self):
        led = self.ledger()
        self.assertEqual(led["broker"]["foreign"], ["OTHER"])

    def test_external_sale_is_detected_and_healed(self):
        led = self.ledger("live")
        self.hold(led, "AAA", qty=1.0)
        self.hold(led, "BBB", qty=1.0)
        out = engine.sync(self.cfg, led, 1000.0, 400.0, {"AAA": 0.4}, at("15:31"))
        self.assertEqual(len(out["incidents"]), 2)
        self.assertEqual(led["positions"]["AAA"]["qty"], 0.4)
        self.assertNotIn("BBB", led["positions"])
        self.assertEqual(led["closed_trades"][-1]["reason"], "external_close_pnl_unknown")


    def test_unrecorded_sell_is_not_mistaken_for_an_external_sale(self):
        led = self.ledger("live")
        self.hold(led, "AAA", qty=1.0)
        led["queue"] = {"date": "2026-11-13", "session": "close", "cash_delta": 0.0, "items": [
            {"id": 1, "symbol": "AAA", "side": "sell", "setup": "dip", "reason": "time_stop",
             "plan_price": 101.0, "qty": 1.0, "status": "released"}]}
        out = engine.sync(self.cfg, led, 1000.0, 400.0, {}, at("15:31"))  # broker already sold it
        self.assertEqual(out["incidents"], [])
        self.assertIn("AAA", led["positions"])
        self.assertEqual(self.planned(led)["action"], "resolve_pending")
        done = engine.fill(self.cfg, led, 1, "filled", at("15:33"), qty=1.0, price=101.0, order_id="o1")
        self.assertTrue(done["ok"])
        self.assertNotIn("AAA", led["positions"])
        self.assertAlmostEqual(led["realized_pnl"], 1.0)


class PlanTests(EngineCase):
    def test_needs_sync_first(self):
        out = engine.plan(self.cfg, lg.new_ledger("paper"), "close", at(), data=self.market())
        self.assertFalse(out["ok"])

    def test_entries_take_breakout_first_then_dip(self):
        out = self.planned(self.ledger())
        self.assertEqual([(o["symbol"], o["setup"]) for o in out["orders"]], [("BRKO", "breakout"), ("DIPP", "dip")])
        self.assertEqual(out["orders"][0]["broker_order"]["dollar_amount"], "50.00", "20% of a $250 sleeve")
        self.assertEqual(out["orders"][0]["broker_order"]["type"], "market")

    def test_no_entries_outside_the_window_or_on_stale_data(self):
        led = self.ledger()
        early = engine.plan(self.cfg, led, "close", at("13:00"), data=self.market())
        self.assertEqual(early["orders"], [])
        stale = self.market()
        stale["SPY"] = stale["SPY"].truncated(stale["SPY"].dates[-2])
        out = engine.plan(self.cfg, led, "close", at(), data=stale)
        self.assertEqual(out["orders"], [])
        self.assertIn("stale", " ".join(out["notes"]))

    def test_foreign_and_locked_symbols_are_not_bought(self):
        led = self.ledger()
        led["broker"]["foreign"].append("BRKO")
        led["locks"]["DIPP"] = "2026-11-18"
        self.assertEqual(self.planned(led)["orders"], [])

    def test_bear_market_blocks_breakouts_and_halves_dips(self):
        data = self.market()
        data["SPY"] = make_series("SPY", start=400.0, drift=-0.002, day_range=0.01)
        out = engine.plan(self.cfg, self.ledger(), "close", at(), data=data)
        self.assertEqual(out["regime"], "bear")
        self.assertEqual([o["symbol"] for o in out["orders"]], ["DIPP"])
        self.assertEqual(out["orders"][0]["broker_order"]["dollar_amount"], "25.00")

    def test_budget_never_exceeds_sleeve_or_buying_power(self):
        led = self.ledger()
        led["broker"]["buying_power"] = 60.0
        out = self.planned(led)
        amounts = [float(o["broker_order"]["dollar_amount"]) for o in out["orders"]]
        self.assertEqual(amounts, [50.0, 10.0])
        led2 = self.ledger()
        self.hold(led2, "CALM", cost=100.0, qty=2.2, entry="2026-11-16")  # $220 of the $250 sleeve deployed today
        led2["last_prices"]["CALM"] = 100.0
        data = self.market()
        out2 = engine.plan(self.cfg, led2, "close", at(), data=data, quotes={"CALM": quote_at(100.0)})
        buys = [o for o in out2["orders"] if o["side"] == "buy"]
        self.assertLessEqual(sum(float(o["broker_order"]["dollar_amount"]) for o in buys), 30.0 + 1e-9)

    def test_stop_and_disaster_exits(self):
        led = self.ledger()
        self.hold(led, "CALM")
        out = self.planned(led, quotes={"CALM": quote_at(89.0)})
        self.assertEqual((out["orders"][0]["side"], out["orders"][0]["reason"]), ("sell", "stop_close"))
        self.assertEqual(out["orders"][0]["id"], 1, "sells come before buys")
        pulse = engine.plan(self.cfg, led, "pulse", at("12:30"), data=self.market(), quotes={"CALM": quote_at(89.0)})
        self.assertEqual(pulse["orders"], [], "the normal stop is not live intraday")
        pulse = engine.plan(self.cfg, led, "pulse", at("12:30"), data=self.market(), quotes={"CALM": quote_at(83.0)})
        self.assertEqual(pulse["orders"][0]["reason"], "disaster_stop")

    def test_time_stop_and_same_day_guard(self):
        led = self.ledger()
        self.hold(led, "CALM", cost=1000.0, entry="2026-11-09")  # 5 trading days ago, under water
        led["positions"]["CALM"].update(stop=1.0, disaster_stop=0.5)
        data = self.market()
        data["CALM"] = make_series("CALM", tail=(-0.01,) * 6)  # still sliding: no snap-back exit
        out = engine.plan(self.cfg, led, "close", at(), data=data)
        self.assertEqual(out["orders"][0]["reason"], "time_stop")
        led2 = self.ledger()
        self.hold(led2, "CALM", entry="2026-11-16")
        out2 = self.planned(led2, quotes={"CALM": quote_at(50.0)})
        self.assertNotIn("sell", [o["side"] for o in out2["orders"]], "no same-day round trips")

    def test_exit_before_earnings(self):
        led = self.ledger()
        self.hold(led, "CALM", cost=1.0)
        led["positions"]["CALM"].update(stop=0.5, disaster_stop=0.2)
        out = self.planned(led, earnings={"CALM": "2026-11-17"})
        self.assertEqual(out["orders"][0]["reason"], "earnings_exit")

    def test_unresolved_live_order_blocks_planning(self):
        led = self.ledger("live")
        led["queue"] = {"date": "2026-11-13", "session": "close", "cash_delta": 0.0,
                        "items": [{"id": 1, "symbol": "X", "side": "buy", "status": "released"}]}
        out = self.planned(led)
        self.assertEqual(out["action"], "resolve_pending")


class ReleaseTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.led = self.ledger()
        self.out = self.planned(self.led)
        self.price = {o["symbol"]: o["plan_price"] for o in self.out["orders"]}

    def release(self, item_id, led=None, when="15:42", **kw):
        led = led or self.led
        sym = next(i["symbol"] for i in led["queue"]["items"] if i["id"] == item_id)
        kw.setdefault("quote", quote_at(self.price[sym]))
        kw.setdefault("review", "ok")
        return engine.release(self.cfg, led, item_id, now=at(when), **kw)

    def test_paper_fill_opens_position_with_stops(self):
        out = self.release(1)
        self.assertEqual(out["action"], "paper_filled")
        pos = self.led["positions"]["BRKO"]
        self.assertAlmostEqual(pos["qty"] * pos["avg_cost"], 50.0, places=2)
        self.assertLess(pos["disaster_stop"], pos["stop"])
        self.assertLess(pos["stop"], pos["avg_cost"])

    def test_orders_must_be_worked_in_order(self):
        self.assertFalse(self.release(2)["ok"])

    def test_wide_spread_blocks_a_buy(self):
        p = self.price["BRKO"]
        out = self.release(1, quote={"bid": p * 0.99, "ask": p * 1.01, "last": p})
        self.assertEqual(out["action"], "skip")
        self.assertIn("spread", [c["check"] for c in out["failed_checks"]])
        self.assertNotIn("BRKO", self.led["positions"])

    def test_price_drift_from_plan_blocks_a_buy(self):
        out = self.release(1, quote=quote_at(self.price["BRKO"] * 1.05))
        self.assertIn("price_matches_plan", [c["check"] for c in out["failed_checks"]])

    def test_broker_alert_blocks(self):
        out = self.release(1, review="alert", alert_text="insufficient buying power")
        self.assertIn("broker_review", [c["check"] for c in out["failed_checks"]])

    def test_earnings_blackout(self):
        self.assertEqual(self.release(1, earnings="2026-11-18")["action"], "skip")
        self.assertEqual(self.release(2, earnings="2026-12-20")["action"], "paper_filled")

    def test_window_closed_blocks(self):
        self.assertEqual(self.release(1, when="15:59")["action"], "skip")

    def test_halt_file_blocks_buys(self):
        open(lg.halt_path(), "w").close()
        out = self.release(1)
        self.assertIn("manual_halt", [c["check"] for c in out["failed_checks"]])

    def test_budget_rechecked_after_each_fill(self):
        self.led["broker"]["buying_power"] = 70.0  # dropped since the plan was made
        self.assertEqual(self.release(1)["action"], "paper_filled")
        self.release(2)
        pos = self.led["positions"]["DIPP"]
        self.assertAlmostEqual(pos["qty"] * pos["avg_cost"], 20.0, delta=0.05)

    def test_live_release_waits_for_fill_before_next_order(self):
        led = self.ledger("live")
        self.planned(led)
        first = self.release(1, led=led)
        self.assertEqual(first["action"], "place")
        self.assertEqual(first["order"], {"symbol": "BRKO", "side": "buy", "type": "market",
                                          "time_in_force": "gfd", "market_hours": "regular_hours",
                                          "dollar_amount": "50.00"})
        self.assertNotIn("BRKO", led["positions"], "nothing is booked until the broker confirms")
        blocked = self.release(2, led=led)
        self.assertFalse(blocked["ok"])
        bad = engine.fill(self.cfg, led, 1, "filled", at("15:43"))
        self.assertFalse(bad["ok"], "a fill needs quantity and price")
        ok = engine.fill(self.cfg, led, 1, "filled", at("15:43"), qty=0.25, price=200.0, order_id="abc")
        self.assertTrue(ok["ok"])
        self.assertEqual(led["positions"]["BRKO"]["qty"], 0.25)
        self.assertEqual(self.release(2, led=led)["action"], "place")
        engine.fill(self.cfg, led, 2, "rejected", at("15:44"))
        self.assertNotIn("DIPP", led["positions"])

    def test_sell_goes_through_wide_spread_and_books_pnl(self):
        led = self.ledger()
        self.hold(led, "CALM", cost=100.0, qty=0.5)
        engine.plan(self.cfg, led, "close", at(), data=self.market(), quotes={"CALM": quote_at(89.0)})
        out = engine.release(self.cfg, led, 1, {"bid": 88.0, "ask": 90.0, "last": 89.0}, "ok", at("15:42"))
        self.assertEqual(out["action"], "paper_filled")
        self.assertNotIn("CALM", led["positions"])
        self.assertAlmostEqual(led["realized_pnl"], -6.0)
        self.assertEqual(led["locks"]["CALM"], "2026-11-19", "stopped-out symbol is locked for 3 trading days")
        self.assertEqual(led["consecutive_losses"], 1)

    def test_live_sell_requires_broker_shares(self):
        led = self.ledger("live")
        self.hold(led, "CALM", cost=100.0, qty=0.5)
        engine.plan(self.cfg, led, "close", at(), data=self.market(), quotes={"CALM": quote_at(89.0)})
        led["broker"]["positions"]["CALM"] = 0.1
        out = engine.release(self.cfg, led, 1, quote_at(89.0), "ok", at("15:42"))
        self.assertIn("broker_holds_shares", [c["check"] for c in out["failed_checks"]])


class BreakerTests(EngineCase):
    def test_daily_loss_limit_blocks_entries(self):
        led = self.ledger()
        led["marks"].append({"date": "2026-11-13", "total_pnl": 0.0, "sleeve_equity": 250.0})
        led["realized_pnl"] = -13.0  # worse than 5% of $250
        out = self.planned(led)
        self.assertEqual(out["orders"], [])
        self.assertTrue(out["breakers"]["block_entries"])

    def test_weekly_and_drawdown(self):
        led = self.ledger()
        for d in ("2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"):
            led["marks"].append({"date": d, "total_pnl": 10.0, "sleeve_equity": 250.0})
        led["peak_pnl"] = 10.0
        led["realized_pnl"] = -16.0  # -26 on the week
        self.assertIn("weekly", " ".join(engine.evaluate_breakers(self.cfg, led, "2026-11-16")["reasons"]))
        led["marks"] = []
        led["peak_pnl"] = 30.0
        led["realized_pnl"] = -10.0  # 16% drawdown: half size
        b = engine.evaluate_breakers(self.cfg, led, "2026-11-16")
        self.assertEqual((b["size_mult"]), 0.5)
        led["peak_pnl"] = 60.0  # 28% drawdown: halt
        self.assertIn("drawdown halt", " ".join(engine.evaluate_breakers(self.cfg, led, "2026-11-16")["reasons"]))

    def test_loss_streak_starts_cooldown(self):
        led = self.ledger()
        for k in range(4):
            sym = f"L{k}"
            self.hold(led, sym, cost=100.0, qty=0.1)
            led["queue"] = {"date": "2026-11-16", "session": "close", "cash_delta": 0.0, "items": [
                {"id": 1, "symbol": sym, "side": "sell", "setup": "dip", "reason": "time_stop",
                 "plan_price": 99.0, "qty": 0.1, "status": "pending"}]}
            engine.release(self.cfg, led, 1, quote_at(99.0), "ok", at("15:42"))
        self.assertEqual(led["cooldown_until"], "2026-11-17")
        self.assertTrue(engine.evaluate_breakers(self.cfg, led, "2026-11-17")["block_entries"])
        self.assertFalse(engine.evaluate_breakers(self.cfg, led, "2026-11-18")["block_entries"])


class ModeAndReportTests(EngineCase):
    def test_live_refused_until_enough_paper_cycles(self):
        self.cfg["execution"]["mode"] = "live"
        mode, note = engine.effective_mode(self.cfg)
        self.assertEqual(mode, "paper")
        self.assertIn("0 of the required 5", note)
        paper = lg.new_ledger("paper")
        paper["cycle_dates"] = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]
        lg.save(paper)
        self.assertEqual(engine.effective_mode(self.cfg), ("live", None))

    def test_report_counts_a_completed_close_cycle_once(self):
        led = self.ledger()
        self.planned(led)
        early = engine.report(self.cfg, led, at("15:45"))
        self.assertEqual(led["cycle_dates"], [], "orders still pending: not a completed cycle")
        self.assertEqual(early["unresolved_order_ids"], [1, 2])
        for item in led["queue"]["items"]:
            engine.release(self.cfg, led, item["id"], quote_at(item["plan_price"]), "ok", at("15:46"))
        engine.report(self.cfg, led, at("15:47"))
        engine.report(self.cfg, led, at("15:48"))
        self.assertEqual(led["cycle_dates"], ["2026-11-16"])
        self.assertEqual(len(led["marks"]), 1)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "STATUS.paper.md")))

    def test_ledger_round_trip_and_journal(self):
        led = self.ledger()
        self.planned(led)
        lg.save(led)
        again = lg.load("paper")
        self.assertEqual(again["queue"]["items"], led["queue"]["items"])
        with open(os.path.join(self.tmp, "journal.jsonl")) as fh:
            events = [line for line in fh]
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(lg.load("live")["positions"], {}, "paper trades never leak into the live book")


if __name__ == "__main__":
    unittest.main()
