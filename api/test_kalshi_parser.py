"""
Tests for Kalshi market price extraction.

Covers the field-fallback chain in extract_kalshi_yes_pct() and
the end-to-end fetch_kalshi_markets() with representative fixtures.
"""

import sys
import os
import unittest
from unittest.mock import patch, MagicMock

# Ensure api/ is importable
sys.path.insert(0, os.path.dirname(__file__))

from scanner import extract_kalshi_yes_pct, safe_float


# ---------------------------------------------------------------------------
# Unit tests for extract_kalshi_yes_pct
# ---------------------------------------------------------------------------

class TestExtractKalshiYesPct(unittest.TestCase):
    """Unit tests for the price-extraction helper."""

    # ── yes_price present ─────────────────────────────────────────────────

    def test_yes_price_cents(self):
        """yes_price in cents (e.g. 42) → 42."""
        m = {"yes_price": 42}
        self.assertEqual(extract_kalshi_yes_pct(m), 42)

    def test_yes_price_float_cents(self):
        """yes_price as float cents (e.g. 42.5) → 42.5."""
        m = {"yes_price": 42.5}
        self.assertEqual(extract_kalshi_yes_pct(m), 42.5)

    def test_yes_price_dollar_fraction(self):
        """yes_price in dollar notation 0.42 → 42."""
        m = {"yes_price": 0.42}
        self.assertAlmostEqual(extract_kalshi_yes_pct(m), 42, places=1)

    def test_yes_price_zero_is_not_valid(self):
        """yes_price=0 should fall through to next fallback."""
        m = {"yes_price": 0, "yes_ask": 55}
        # Should NOT return 0; should fall through to yes_ask
        result = extract_kalshi_yes_pct(m)
        self.assertGreater(result, 0)

    def test_yes_price_string(self):
        """yes_price as string '65' → 65."""
        m = {"yes_price": "65"}
        self.assertEqual(extract_kalshi_yes_pct(m), 65)

    # ── bid/ask both present ──────────────────────────────────────────────

    def test_both_bid_ask(self):
        """Both yes_bid and yes_ask present → midpoint."""
        m = {"yes_bid": 40, "yes_ask": 50}
        self.assertEqual(extract_kalshi_yes_pct(m), 45)

    def test_both_bid_ask_dollars(self):
        """Both bid/ask in dollar notation → midpoint in cents."""
        m = {"yes_bid": 0.40, "yes_ask": 0.50}
        self.assertAlmostEqual(extract_kalshi_yes_pct(m), 45, places=1)

    # ── one-sided book (THE BUG) ──────────────────────────────────────────

    def test_bid_zero_ask_present(self):
        """yes_bid=0, yes_ask=55 → should use ask (the critical bug case)."""
        m = {"yes_bid": 0, "yes_ask": 55}
        result = extract_kalshi_yes_pct(m)
        self.assertGreater(result, 0)
        # With only ask available, we use the ask directly
        self.assertEqual(result, 55)

    def test_bid_present_ask_zero(self):
        """yes_bid=30, yes_ask=0 → should use bid."""
        m = {"yes_bid": 30, "yes_ask": 0}
        result = extract_kalshi_yes_pct(m)
        self.assertEqual(result, 30)

    def test_bid_present_ask_missing(self):
        """yes_bid=30, no yes_ask key → should use bid."""
        m = {"yes_bid": 30}
        result = extract_kalshi_yes_pct(m)
        self.assertEqual(result, 30)

    def test_bid_missing_ask_present(self):
        """no yes_bid key, yes_ask=55 → should use ask."""
        m = {"yes_ask": 55}
        result = extract_kalshi_yes_pct(m)
        self.assertEqual(result, 55)

    # ── last_price fallback ───────────────────────────────────────────────

    def test_last_price_fallback(self):
        """No bid/ask/yes_price → last_price should be used."""
        m = {"last_price": 72}
        self.assertEqual(extract_kalshi_yes_pct(m), 72)

    def test_last_price_dollar(self):
        """last_price in dollar notation → converted to cents."""
        m = {"last_price": 0.72}
        self.assertAlmostEqual(extract_kalshi_yes_pct(m), 72, places=1)

    # ── dollar-denominated field variants ─────────────────────────────────

    def test_yes_price_dollar_field(self):
        """Explicit 'yes_price_dollar' field → cents."""
        m = {"yes_price_dollar": 0.55}
        self.assertAlmostEqual(extract_kalshi_yes_pct(m), 55, places=1)

    def test_last_price_dollar_field(self):
        """Explicit 'last_price_dollar' field → cents."""
        m = {"last_price_dollar": 0.33}
        self.assertAlmostEqual(extract_kalshi_yes_pct(m), 33, places=1)

    # ── validation: out-of-range prices ───────────────────────────────────

    def test_negative_price_returns_none(self):
        """Negative yes_price → None (invalid)."""
        m = {"yes_price": -5}
        self.assertIsNone(extract_kalshi_yes_pct(m))

    def test_over_100_returns_none(self):
        """yes_price > 100 → None (invalid, unless dollar normalization applies)."""
        m = {"yes_price": 150}
        self.assertIsNone(extract_kalshi_yes_pct(m))

    def test_exactly_zero_all_fields_returns_none(self):
        """All fields zero → None."""
        m = {"yes_price": 0, "yes_bid": 0, "yes_ask": 0, "last_price": 0}
        self.assertIsNone(extract_kalshi_yes_pct(m))

    def test_empty_market_returns_none(self):
        """No relevant fields → None."""
        m = {"ticker": "FOO", "title": "Something"}
        self.assertIsNone(extract_kalshi_yes_pct(m))

    # ── edge cases ────────────────────────────────────────────────────────

    def test_none_values_everywhere(self):
        """All price fields explicitly None → None."""
        m = {"yes_price": None, "yes_bid": None, "yes_ask": None, "last_price": None}
        self.assertIsNone(extract_kalshi_yes_pct(m))

    def test_non_numeric_strings(self):
        """Non-numeric strings → None."""
        m = {"yes_price": "N/A", "yes_bid": "---"}
        self.assertIsNone(extract_kalshi_yes_pct(m))

    def test_yes_price_preferred_over_bid_ask(self):
        """When yes_price and bid/ask both exist, yes_price wins."""
        m = {"yes_price": 60, "yes_bid": 40, "yes_ask": 50}
        self.assertEqual(extract_kalshi_yes_pct(m), 60)


# ---------------------------------------------------------------------------
# Integration test: fetch_kalshi_markets with fixture data
# ---------------------------------------------------------------------------

# Representative fixture mimicking real Kalshi API responses
KALSHI_FIXTURE = {
    "markets": [
        # Case 1: yes_price present (normal)
        {"ticker": "MKT-A", "title": "Market A", "event_ticker": "EVT-A",
         "yes_price": 65, "volume": 1000, "open_interest": 500,
         "category": "Politics", "close_time": "2026-06-01T00:00:00Z"},

        # Case 2: both bid and ask
        {"ticker": "MKT-B", "title": "Market B", "event_ticker": "EVT-B",
         "yes_bid": 40, "yes_ask": 50, "volume": 800, "open_interest": 300,
         "category": "Economics", "close_time": "2026-07-01T00:00:00Z"},

        # Case 3: one-sided book — bid=0, ask present (THE BUG)
        {"ticker": "MKT-C", "title": "Market C", "event_ticker": "EVT-C",
         "yes_bid": 0, "yes_ask": 72, "volume": 200, "open_interest": 100,
         "category": "Tech", "close_time": "2026-08-01T00:00:00Z"},

        # Case 4: only last_price
        {"ticker": "MKT-D", "title": "Market D", "event_ticker": "EVT-D",
         "last_price": 55, "volume": 150, "open_interest": 80,
         "category": "Sports", "close_time": "2026-09-01T00:00:00Z"},

        # Case 5: dollar-denominated yes_price (0.42)
        {"ticker": "MKT-E", "title": "Market E", "event_ticker": "EVT-E",
         "yes_price": 0.42, "volume": 500, "open_interest": 200,
         "category": "Other", "close_time": "2026-10-01T00:00:00Z"},

        # Case 6: completely empty book — should be excluded
        {"ticker": "MKT-F", "title": "Market F", "event_ticker": "EVT-F",
         "volume": 50, "open_interest": 10,
         "category": "Other", "close_time": "2026-11-01T00:00:00Z"},
    ],
    "cursor": "",
}


class TestFetchKalshiMarketsIntegration(unittest.TestCase):
    """Integration tests that mock the HTTP layer and verify market parsing."""

    def _mock_fetch(self, fixture):
        """Patch fetch_json to return fixture data."""
        return patch("scanner.fetch_json", return_value=fixture)

    def test_nonzero_markets_from_sparse_data(self):
        """fetch_kalshi_markets returns non-zero valid markets from sparse fixtures."""
        from scanner import fetch_kalshi_markets
        with self._mock_fetch(KALSHI_FIXTURE):
            markets = fetch_kalshi_markets()
        # Should accept cases 1-5, reject case 6
        self.assertGreaterEqual(len(markets), 5)

    def test_one_sided_book_included(self):
        """Market with bid=0, ask=72 must NOT be dropped."""
        from scanner import fetch_kalshi_markets
        with self._mock_fetch(KALSHI_FIXTURE):
            markets = fetch_kalshi_markets()
        tickers = [m.get("ticker") for m in markets]
        self.assertIn("MKT-C", tickers, "One-sided book market was incorrectly dropped")

    def test_last_price_fallback_included(self):
        """Market with only last_price must be included."""
        from scanner import fetch_kalshi_markets
        with self._mock_fetch(KALSHI_FIXTURE):
            markets = fetch_kalshi_markets()
        tickers = [m.get("ticker") for m in markets]
        self.assertIn("MKT-D", tickers, "last_price fallback market was incorrectly dropped")

    def test_dollar_price_normalized(self):
        """Market with yes_price=0.42 should have yes_price around 42 (cents)."""
        from scanner import fetch_kalshi_markets
        with self._mock_fetch(KALSHI_FIXTURE):
            markets = fetch_kalshi_markets()
        mkt_e = next((m for m in markets if m.get("ticker") == "MKT-E"), None)
        self.assertIsNotNone(mkt_e, "Dollar-notation market was dropped")
        self.assertAlmostEqual(mkt_e["yes_price"], 42, delta=1)

    def test_empty_book_excluded(self):
        """Market with no price fields should be excluded."""
        from scanner import fetch_kalshi_markets
        with self._mock_fetch(KALSHI_FIXTURE):
            markets = fetch_kalshi_markets()
        tickers = [m.get("ticker") for m in markets]
        self.assertNotIn("MKT-F", tickers, "Empty-book market should have been excluded")

    def test_all_prices_in_valid_range(self):
        """Every accepted market must have yes_price in (0, 100]."""
        from scanner import fetch_kalshi_markets
        with self._mock_fetch(KALSHI_FIXTURE):
            markets = fetch_kalshi_markets()
        for m in markets:
            self.assertGreater(m["yes_price"], 0, f"{m['ticker']} has yes_price <= 0")
            self.assertLessEqual(m["yes_price"], 100, f"{m['ticker']} has yes_price > 100")


# Also test bots.py's copy of fetch_kalshi_markets
class TestBotsKalshiIntegration(unittest.TestCase):
    """Verify bots.py fetch_kalshi_markets also works with sparse data."""

    def test_nonzero_markets_bots(self):
        from bots import fetch_kalshi_markets
        with patch("bots.fetch_json", return_value=KALSHI_FIXTURE):
            markets = fetch_kalshi_markets()
        # Should accept cases 1-5, reject case 6
        self.assertGreaterEqual(len(markets), 5)

    def test_one_sided_book_bots(self):
        from bots import fetch_kalshi_markets
        with patch("bots.fetch_json", return_value=KALSHI_FIXTURE):
            markets = fetch_kalshi_markets()
        tickers = [m.get("ticker") for m in markets]
        self.assertIn("MKT-C", tickers, "bots.py: one-sided book market was dropped")

    def test_prices_in_valid_range_bots(self):
        from bots import fetch_kalshi_markets
        with patch("bots.fetch_json", return_value=KALSHI_FIXTURE):
            markets = fetch_kalshi_markets()
        for m in markets:
            self.assertGreater(m["yes_pct"], 0, f"bots.py: {m['ticker']} has yes_pct <= 0")
            self.assertLessEqual(m["yes_pct"], 100, f"bots.py: {m['ticker']} has yes_pct > 100")


if __name__ == "__main__":
    unittest.main()
