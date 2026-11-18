"""Tests for conservative public Eco catalog prices; no network access."""
import sys
from pathlib import Path
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pricing

STORAGE = "hybridsoftraid-2x4000sa-2x500nvme-" + pricing.PLAN

def item(code, amount):
    return {
        "planCode": code,
        "pricings": [
            {"duration": "P1M", "pricingMode": "default", "price": amount},
            {"duration": "P12M", "pricingMode": "default", "price": amount * 10},
        ]
    }

def fixture():
    return {
        "locale": {"currencyCode": "EUR"},
        "plans": [{
            **item(pricing.PLAN, 2699000000),
            "addonFamilies": [
                {"name": "storage", "mandatory": True, "addons": [STORAGE], "default": STORAGE},
                {"name": "memory", "mandatory": True, "addons": ["ram-128"], "default": "ram-128"},
                {"name": "bandwidth", "mandatory": True, "addons": ["bw-500"], "default": "bw-500"},
            ],
        }],
        "addons": [item(STORAGE, 500000000), item("ram-128", 0), item("bw-500", 0)]
    }

class PriceTests(unittest.TestCase):
    def test_complete_price_without_tax(self):
        prices = pricing.extract_prices(fixture())
        self.assertEqual(prices["hybridsoftraid-2x4000sa-2x500nvme"],
                         "31.99 EUR/mês sem IVA (estimativa de catálogo)")

    def test_missing_mandatory_addon_hides_price(self):
        data = fixture()
        data["addons"] = data["addons"][:-1]
        self.assertEqual(pricing.extract_prices(data), {})

    def test_ambiguous_monthly_quote_hides_price(self):
        data = fixture()
        data["addons"][0]["pricings"].append(data["addons"][0]["pricings"][0].copy())
        self.assertEqual(pricing.extract_prices(data), {})

    def test_non_eur_catalog_rejected(self):
        data = fixture()
        data["locale"]["currencyCode"] = "USD"
        with self.assertRaises(ValueError):
            pricing.extract_prices(data)

    def test_cache_fresh_and_stale(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "pricing-cache.json"
            with patch.object(pricing, "fetch_catalog", return_value=fixture()) as fetch:
                values = pricing.cached_prices(path, now=1000)
                self.assertEqual(len(values), 1)
                pricing.cached_prices(path, now=1001)
                self.assertEqual(fetch.call_count, 1)
                pricing.cached_prices(path, now=1000 + 6 * 3600 + 1)
                self.assertEqual(fetch.call_count, 2)

    def test_catalog_failure_does_not_produce_stale_price(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "pricing-cache.json"
            pricing.save_cache(path, {"checked_at": 1, "prices": {"fake": "wrong"}})
            with patch.object(pricing, "fetch_catalog", side_effect=TimeoutError("offline")):
                self.assertEqual(pricing.cached_prices(path, now=6 * 3600 + 10), {})
                self.assertEqual(pricing.cached_prices(path, now=6 * 3600 + 11), {})

if __name__ == "__main__":
    unittest.main()
