"""Offline item-price evidence tests; no OVH requests or payments."""

import sys
from pathlib import Path
import unittest
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from renewal_pricing import inspect_cart_renewals


def item(value: str = "27.00", *, currency: str = "EUR", duration: str = "P1M") -> dict:
    return {"duration": duration, "prices": [{"label": "RENEW", "price": {
        "value": value, "currencyCode": currency,
    }}]}


class RenewalEvidenceTests(unittest.TestCase):
    def test_complete_item_prices_are_only_evidence_not_vat_verified(self):
        result = inspect_cart_renewals([item("27.00"), item("3.00"), item("2.50"), item("0")])
        self.assertEqual(result.amount_eur, Decimal("32.50"))
        self.assertEqual(result.components, 4)
        self.assertIn("VAT status", result.summary())
        self.assertIn("NOT VERIFIED", result.summary())
        self.assertIn("NOT a checkout renewal quote", result.summary())

    def test_missing_addon_price_makes_whole_sum_unknown(self):
        result = inspect_cart_renewals([item(), {"itemId": 42}])
        self.assertIsNone(result.amount_eur)
        self.assertNotIn("27.00", result.summary())
        self.assertIn("unavailable/incomplete", result.summary())

    def test_ambiguous_renewal_rows_are_rejected(self):
        doubled = item()
        doubled["prices"].append(doubled["prices"][0].copy())
        self.assertIsNone(inspect_cart_renewals([doubled]).amount_eur)

    def test_currency_and_duration_must_match(self):
        self.assertIsNone(inspect_cart_renewals([item(currency="CAD")]).amount_eur)
        self.assertIsNone(inspect_cart_renewals([item(duration="P1Y")]).amount_eur)

    def test_rejects_untrusted_numbers_and_booleans(self):
        for value in ("37.001", "NaN", "1e2", "-1", True, None):
            with self.subTest(value=value):
                self.assertIsNone(inspect_cart_renewals([item(value)]).amount_eur)

    def test_missing_rows_are_not_interpreted_as_zero(self):
        for data in ([], [{}], [None], [{"prices": []}], [{"prices": "RENEW"}]):
            with self.subTest(data=data):
                self.assertIsNone(inspect_cart_renewals(data).amount_eur)


if __name__ == "__main__":
    unittest.main()
