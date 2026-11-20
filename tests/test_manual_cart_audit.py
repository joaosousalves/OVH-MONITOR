"""Offline, privacy-safe manual cart commercial report tests."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cart_identity import CartIdentityEvidence
from manual_cart_audit import manual_cart_report


def quote(*, gross="41.80", tax="7.82", net="33.98"):
    return {
        "prices": {
            "withoutTax": {"currencyCode": "EUR", "value": net},
            "tax": {"currencyCode": "EUR", "value": tax},
            "withTax": {"currencyCode": "EUR", "value": gross,
                        "text": "PRIVATE-SERVER-ACCOUNT-AND-PRICE"},
        },
        "details": [
            {"detailType": "INSTALLATION", "totalPrice": {
                "currencyCode": "EUR", "value": "10.00"},
             "description": "PRIVATE-CUSTOMER-ADDRESS"},
            {"detailType": "DURATION", "totalPrice": {
                "currencyCode": "EUR", "value": "23.98"}},
        ],
        "contracts": [{"url": "https://example.test/private-contract-URL"}],
        "commitmentDuration": "P0M",
    }


def item(net="20.00", gross="24.60"):
    return {"duration": "P1M", "priceToken": "PRIVATE-SECRET", "prices": [{
        "label": "RENEW",
        "price": {"currencyCode": "EUR", "value": net},
        "priceWithTax": {"currencyCode": "EUR", "value": gross},
    }]}


class ManualCartAuditTests(unittest.TestCase):
    def test_complete_report_itemizes_current_initial_and_indicative_monthly(self):
        result = manual_cart_report(quote(), (item(), item("5.00", "6.15")),
                                    CartIdentityEvidence("VERIFIED", "exact cart readback matches"))
        self.assertIn("33.98 EUR net + 7.82 EUR tax = 41.80 EUR", result)
        self.assertIn("listed amount 10.00 EUR", result)
        self.assertIn("RENEW component sum 25.00 EUR", result)
        self.assertIn("ITEM RENEW WITH VAT: 30.75 EUR", result)
        self.assertIn("CART IDENTITY VERIFIED", result)
        self.assertIn("zero indicator seen", result)
        self.assertIn("legal text NOT VERIFIED", result)
        for secret in ("PRIVATE-CUSTOMER-ADDRESS", "PRIVATE-SECRET", "PRIVATE-SERVER",
                       "private-contract-URL"):
            self.assertNotIn(secret, result)
        self.assertIn("Checkout and payment remain DISABLED", result)

    def test_incomplete_quote_never_invents_initial_or_monthly(self):
        result = manual_cart_report({"prices": {"withTax": {"text": "41.80 EUR"}}},
                                    ({"itemId": 1},))
        self.assertIn("INITIAL VAT: UNVERIFIED", result)
        self.assertIn("BREAKDOWN UNVERIFIED", result)
        self.assertIn("WITH VAT: UNVERIFIED", result)
        self.assertIn("NO FIXED COMMITMENT: UNVERIFIED", result)
        self.assertIn("CART IDENTITY UNVERIFIED", result)
        self.assertNotIn("41.80 EUR net", result)

    def test_conflicting_arithmetic_and_gross_monthly_flagged(self):
        bad = quote(tax="7.81")
        result = manual_cart_report(bad, (item("40.00", "20.00"),))
        self.assertIn("INITIAL VAT: CONFLICT", result)
        self.assertIn("WITH VAT: CONFLICT", result)
        self.assertNotIn("future monthly invoice VERIFIED", result)

    def test_fixed_commitment_indicator_is_never_approved(self):
        bad = quote()
        bad["commitmentDuration"] = "P12M"
        result = manual_cart_report(bad, (item(),))
        self.assertIn("NO FIXED COMMITMENT: CONFLICT", result)
        self.assertNotIn("zero indicator seen", result)


if __name__ == "__main__":
    unittest.main()
