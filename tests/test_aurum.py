import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aurum.bars import Bar, IST  # noqa: E402
from aurum.config import CostConfig, RiskConfig, RunConfig  # noqa: E402
from aurum.costs import CostModel  # noqa: E402
from aurum.engine import BacktestEngine  # noqa: E402
from aurum.models import Side  # noqa: E402
from aurum.paper import PaperEngine  # noqa: E402
from aurum.strategies import DonchianTrend, make_strategy  # noqa: E402
from aurum.stocks import adjust_for_corporate_actions  # noqa: E402
import aurum.tokens as tokens  # noqa: E402


def synth(prices: list[float], symbol: str = "TEST") -> list[Bar]:
    bars = []
    for index, price in enumerate(prices):
        stamp = datetime(2020, 1, 1, tzinfo=IST) + timedelta(days=index)
        bars.append(
            Bar(
                timestamp=stamp,
                symbol=symbol,
                open=price,
                high=price * 1.004,
                low=price * 0.996,
                close=price,
                volume=1_000,
            )
        )
    return bars


def upward_then_flat() -> list[float]:
    prices = [100.0]
    for _ in range(40):
        prices.append(prices[-1] * 1.01)
    for _ in range(15):
        prices.append(prices[-1] * 0.995)
    return prices


class CostTests(unittest.TestCase):
    def test_cost_increases_with_turnover(self) -> None:
        model = CostModel()
        small = model.charges(False, 100.0, 1, 1.0).total
        large = model.charges(False, 100.0, 10, 1.0).total
        self.assertGreater(large, small)

    def test_stt_only_on_sell(self) -> None:
        model = CostModel()
        buy = model.charges(True, 100.0, 5, 1.0)
        sell = model.charges(False, 100.0, 5, 1.0)
        self.assertEqual(buy.stt, 0.0)
        self.assertGreater(sell.stt, 0.0)
        self.assertGreater(buy.stamp, 0.0)

    def test_slippage_direction(self) -> None:
        model = CostModel(CostConfig(slippage_bps=10.0))
        self.assertGreater(model.fill_price(100.0, True), 100.0)
        self.assertLess(model.fill_price(100.0, False), 100.0)


class StrategyTests(unittest.TestCase):
    def test_make_strategy_registry(self) -> None:
        config = RunConfig(strategy="donchian")
        self.assertIsInstance(make_strategy(config), DonchianTrend)
        with self.assertRaises(ValueError):
            make_strategy(RunConfig(strategy="nope"))

    def test_donchian_emits_long_on_breakout(self) -> None:
        bars = synth(upward_then_flat())
        config = RunConfig(strategy="donchian", params={"entry": 10, "exit": 5, "regime_sma": 0, "atr_period": 5})
        strategy = make_strategy(config)
        signal = None
        for index in range(20, len(bars)):
            signal = strategy.signal(bars[: index + 1])
            if signal is not None:
                break
        self.assertIsNotNone(signal)
        self.assertIs(signal.side, Side.LONG)
        self.assertGreater(signal.stop_distance, 0)


class EngineTests(unittest.TestCase):
    def base_config(self, **params) -> RunConfig:
        return RunConfig(
            symbol="TEST",
            timeframe="1d",
            bar_minutes=375,
            intraday_only=False,
            strategy="donchian",
            params={"entry": 10, "exit": 5, "regime_sma": 0, "atr_period": 5, **params},
            costs=CostConfig(),
            risk=RiskConfig(capital=1_000_000, risk_per_trade_pct=0.01, multiplier=1.0, max_quantity=50, max_trades_per_day=1),
        )

    def test_equity_reconciles_with_trades(self) -> None:
        bars = synth(upward_then_flat())
        result = BacktestEngine(self.base_config()).run(bars)
        expected = result.initial_capital + sum(trade.net_pnl for trade in result.trades)
        self.assertAlmostEqual(result.final_equity, expected, places=4)
        self.assertGreater(len(result.trades), 0)

    def test_entries_fill_on_the_bar_after_the_signal(self) -> None:
        bars = synth(upward_then_flat())
        result = BacktestEngine(self.base_config()).run(bars)
        index_of = {bar.timestamp: index for index, bar in enumerate(bars)}
        signal_times = {signal.timestamp for signal in result.signals}
        for trade in result.trades:
            entry_index = index_of[trade.entry_time]
            self.assertIn(bars[entry_index - 1].timestamp, signal_times)

    def test_stop_is_checked_before_target(self) -> None:
        prices = [100.0] * 30 + [101.0, 102.0, 103.0, 104.0, 105.0]
        bars = synth(prices)
        config = self.base_config(stop_atr=1.0, target_r=0.5)
        result = BacktestEngine(config).run(bars)
        for trade in result.trades:
            if trade.exit_reason in {"stop_loss", "take_profit"}:
                self.assertIn(trade.exit_reason, {"stop_loss", "take_profit", "gap_stop", "gap_target"})


class PaperTests(unittest.TestCase):
    def test_paper_engine_is_paper_only_and_persists(self) -> None:
        bars = synth(upward_then_flat())
        config = RunConfig(
            symbol="TEST",
            strategy="donchian",
            bar_minutes=375,
            intraday_only=False,
            params={"entry": 10, "exit": 5, "regime_sma": 0, "atr_period": 5},
            risk=RiskConfig(capital=1_000_000, max_trades_per_day=1),
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            engine = PaperEngine(config, path)
            engine.initialize_history(bars[:30])
            for bar in bars[30:]:
                engine.process_bar(bar)
            self.assertEqual(engine.state["mode"], "PAPER_ONLY")
            self.assertEqual(engine.state["broker"], "none (paper)")
            self.assertEqual(engine.state["last_timestamp"], bars[-1].timestamp.isoformat())
            reloaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(reloaded["mode"], "PAPER_ONLY")
            self.assertIn("trades", reloaded)


class StockDataTests(unittest.TestCase):
    def test_split_jump_is_back_adjusted(self) -> None:
        prices = [100.0] * 10 + [10.0] * 10  # 1:10 split artifact
        bars = synth(prices)
        cleaned = adjust_for_corporate_actions(bars)
        worst = max(
            abs(cleaned[i].close / cleaned[i - 1].close - 1.0) for i in range(1, len(cleaned))
        )
        self.assertLess(worst, 0.05)

    def test_isolated_bad_print_is_flattened(self) -> None:
        prices = [100.0] * 5 + [1.0] + [100.0] * 5
        bars = synth(prices)
        cleaned = adjust_for_corporate_actions(bars)
        worst = max(
            abs(cleaned[i].close / cleaned[i - 1].close - 1.0) for i in range(1, len(cleaned))
        )
        self.assertLess(worst, 0.05)

    def test_real_moves_are_preserved(self) -> None:
        prices = [100.0, 100.0, 90.0, 94.0, 88.0]
        bars = synth(prices)
        cleaned = adjust_for_corporate_actions(bars)
        self.assertAlmostEqual(cleaned[2].close / cleaned[1].close - 1.0, -0.10, places=6)


class RiskSizingTests(unittest.TestCase):
    def test_notional_sizing_ignores_confidence(self) -> None:
        from aurum.risk import RiskManager
        from aurum.config import RiskConfig

        manager = RiskManager(RiskConfig(sizing_mode="notional", target_notional_pct=2.0, max_notional_pct=4.0,
                                         max_margin_pct=0.9, margin_pct=0.15, max_quantity=10, multiplier=1.0))
        low_conf = manager.size(1_000_000, 20_000, 300, confidence=0.6)
        high_conf = manager.size(1_000_000, 20_000, 300, confidence=1.0)
        self.assertEqual(low_conf.quantity, high_conf.quantity)
        self.assertEqual(low_conf.quantity, 10)  # 2x notional, capped by max_quantity

    def test_risk_sizing_scales_with_confidence(self) -> None:
        from aurum.risk import RiskManager
        from aurum.config import RiskConfig

        manager = RiskManager(RiskConfig(capital=1_000_000, risk_per_trade_pct=0.01, multiplier=1.0))
        low = manager.size(1_000_000, 20_000, 300, confidence=0.6).quantity
        high = manager.size(1_000_000, 20_000, 300, confidence=1.0).quantity
        self.assertGreater(high, low)


class TokenHygieneTests(unittest.TestCase):
    def test_module_has_no_order_capabilities(self) -> None:
        source = Path(tokens.__file__).read_text(encoding="utf-8").lower()
        for forbidden in ("place_order", "modify_order", "cancel_order", "/orders"):
            self.assertNotIn(forbidden, source)

    def test_dhan_post_only_allows_historical_endpoints(self) -> None:
        with self.assertRaises(tokens.TokenError):
            tokens.dhan_post("https://api.dhan.co/v2/orders", {})
        with self.assertRaises(tokens.TokenError):
            tokens.dhan_post("https://api.dhan.co/v2/charts/intraday", {"orderType": "BUY"})

    def test_cache_is_used_when_fresh(self) -> None:
        with TemporaryDirectory() as directory:
            original = tokens.CACHE_PATH
            tokens.CACHE_PATH = Path(directory) / ".broker_tokens.json"
            try:
                tokens.CACHE_PATH.write_text(
                    json.dumps(
                        {
                            "dhan": {
                                "token": "cached-token",
                                "expiry": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                                "fetched_at": datetime.now(timezone.utc).isoformat(),
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                with patch.object(tokens, "_api_request", side_effect=AssertionError("network used")):
                    self.assertEqual(tokens.get_token("dhan"), "cached-token")
            finally:
                tokens.CACHE_PATH = original

    def test_expired_cache_triggers_refetch(self) -> None:
        with TemporaryDirectory() as directory:
            original = tokens.CACHE_PATH
            tokens.CACHE_PATH = Path(directory) / ".broker_tokens.json"
            try:
                tokens.CACHE_PATH.write_text(
                    json.dumps(
                        {
                            "dhan": {
                                "token": "stale",
                                "expiry": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                                "fetched_at": (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(),
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                payload = {"token": "fresh", "expiry": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()}
                with patch.object(tokens, "_api_request", return_value=payload):
                    self.assertEqual(tokens.get_token("dhan"), "fresh")
            finally:
                tokens.CACHE_PATH = original

    def test_api_key_never_leaks_into_cache(self) -> None:
        with TemporaryDirectory() as directory:
            original = tokens.CACHE_PATH
            tokens.CACHE_PATH = Path(directory) / ".broker_tokens.json"
            try:
                payload = {"token": "xyz", "expiry": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()}
                with patch.object(tokens, "_api_request", return_value=payload):
                    tokens.get_token("groww")
                text = tokens.CACHE_PATH.read_text(encoding="utf-8")
                self.assertNotIn("TOKEN_API_KEY", text)
                self.assertNotIn(os.environ.get("TOKEN_API_KEY", "definitely-not-set"), text)
            finally:
                tokens.CACHE_PATH = original


if __name__ == "__main__":
    unittest.main()
