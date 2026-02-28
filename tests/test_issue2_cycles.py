import unittest
from unittest.mock import patch

import api.bots as bots


def _base_bot(strategy="test_strategy", max_positions=1):
    return {
        "id": "testbot",
        "name": "Test Bot",
        "strategy": strategy,
        "strategy_name": "Test Strategy",
        "title": "Test Title",
        "personality": "Test",
        "emoji": "T",
        "color": "#111111",
        "accent": "#222222",
        "params": {"max_positions": max_positions, "kelly_fraction": 0.25},
    }


def _trade(market="Will test happen?"):
    return {
        "market": market,
        "platform": "Polymarket",
        "url": "https://example.com",
        "category": "Other",
        "direction": "BUY_YES",
        "market_price": 40.0,
        "estimated_prob": 55.0,
        "edge_pp": 15.0,
        "bet_amount": 100.0,
        "kelly_fraction": 0.25,
        "source": "Test Source",
        "rationale": "Test rationale",
        "confidence": "HIGH",
    }


class ThinkingCyclesTests(unittest.TestCase):
    def test_run_bot_engine_emits_latest_cycle_for_hold_with_position_cap_reason(self):
        bot = _base_bot(strategy="test_strategy", max_positions=1)
        existing_pos = {
            "trade_id": "p1",
            "market": "Will test happen?",
            "direction": "BUY_YES",
            "entry_price": 40.0,
            "current_price": 40.0,
            "bet_amount": 100.0,
            "timestamp": "2026-02-28T10:00:00Z",
            "platform": "Polymarket",
            "url": "https://example.com",
        }
        state = {
            bot["id"]: {
                "bankroll": 9900.0,
                "total_trades": 1,
                "winning_trades": 0,
                "total_pnl": 0,
                "peak_bankroll": 10000.0,
                "positions": [existing_pos],
                "equity_curve": [{"time": "2026-02-28T10:00:00Z", "value": 10000.0}],
            }
        }

        with patch.object(bots, "BOTS", [bot]), \
             patch.object(bots, "STRATEGY_MAP", {"test_strategy": lambda *args, **kwargs: [_trade()]}), \
             patch.object(bots, "fetch_polymarket_markets", return_value=[]), \
             patch.object(bots, "fetch_kalshi_markets", return_value=[]), \
             patch.object(bots, "load_state", return_value=state), \
             patch.object(bots, "load_trades", return_value=[]), \
             patch.object(bots, "save_state"), \
             patch.object(bots, "save_trades"), \
             patch.object(bots, "load_cycles", return_value=[]), \
             patch.object(bots, "save_cycles"):
            result = bots.run_bot_engine()

        cycle = result["bots"][0]["latest_cycle"]
        self.assertEqual(cycle["decision"], "HOLD")
        reasons = {r["reason"] for r in cycle["top_hold_reasons"]}
        self.assertIn("AT_MAX_POSITIONS", reasons)
        self.assertIn("recentCycles", result)
        self.assertGreaterEqual(len(result["recentCycles"]), 1)

    def test_run_bot_engine_emits_dependency_reason_for_arb_bot_when_kalshi_missing(self):
        bot = _base_bot(strategy="cross_platform_arb", max_positions=2)
        state = {
            bot["id"]: {
                "bankroll": 10000.0,
                "total_trades": 0,
                "winning_trades": 0,
                "total_pnl": 0,
                "peak_bankroll": 10000.0,
                "positions": [],
                "equity_curve": [{"time": "2026-02-28T10:00:00Z", "value": 10000.0}],
            }
        }

        with patch.object(bots, "BOTS", [bot]), \
             patch.object(bots, "STRATEGY_MAP", {"cross_platform_arb": lambda *args, **kwargs: []}), \
             patch.object(bots, "fetch_polymarket_markets", return_value=[{"title": "X", "yes_pct": 50.0, "volume": 1000, "platform": "Polymarket", "url": "https://example.com"}]), \
             patch.object(bots, "fetch_kalshi_markets", return_value=[]), \
             patch.object(bots, "load_state", return_value=state), \
             patch.object(bots, "load_trades", return_value=[]), \
             patch.object(bots, "save_state"), \
             patch.object(bots, "save_trades"), \
             patch.object(bots, "load_cycles", return_value=[]), \
             patch.object(bots, "save_cycles"):
            result = bots.run_bot_engine()

        cycle = result["bots"][0]["latest_cycle"]
        reasons = {r["reason"] for r in cycle["top_hold_reasons"]}
        self.assertIn("DEPENDENCY_DATA_UNAVAILABLE", reasons)
        self.assertEqual(cycle["decision"], "HOLD")

    def test_run_bot_engine_marks_trade_cycle_when_trade_executes(self):
        bot = _base_bot(strategy="test_strategy", max_positions=2)
        state = {
            bot["id"]: {
                "bankroll": 10000.0,
                "total_trades": 0,
                "winning_trades": 0,
                "total_pnl": 0,
                "peak_bankroll": 10000.0,
                "positions": [],
                "equity_curve": [{"time": "2026-02-28T10:00:00Z", "value": 10000.0}],
            }
        }

        with patch.object(bots, "BOTS", [bot]), \
             patch.object(bots, "STRATEGY_MAP", {"test_strategy": lambda *args, **kwargs: [_trade("Will another test happen?")]}), \
             patch.object(bots, "fetch_polymarket_markets", return_value=[]), \
             patch.object(bots, "fetch_kalshi_markets", return_value=[]), \
             patch.object(bots, "load_state", return_value=state), \
             patch.object(bots, "load_trades", return_value=[]), \
             patch.object(bots, "save_state"), \
             patch.object(bots, "save_trades"), \
             patch.object(bots, "load_cycles", return_value=[]), \
             patch.object(bots, "save_cycles"):
            result = bots.run_bot_engine()

        cycle = result["bots"][0]["latest_cycle"]
        self.assertEqual(cycle["decision"], "TRADE")
        self.assertEqual(cycle["top_hold_reasons"], [])


if __name__ == "__main__":
    unittest.main()
