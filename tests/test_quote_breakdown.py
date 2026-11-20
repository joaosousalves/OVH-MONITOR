"""Offline tests for privacy-safe, conservative checkout price details."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import commercial_audit as audit
from account_reconciliation import AccountInventory
from ksle_monitor import Stock
from payment_evidence import PaymentEvidence
from pending_orders import PendingOrderInventory
from purchase_guard import APPROVED_FQNS, STORAGE_PRIORITIES, OrderJournal, PurchasePolicy
from purchase_quota import PurchaseQuota
import quote_breakdown as breakdown


def line(kind: str, value: str, currency: str = "EUR") -> dict:
    return {
        "detailType": kind,
        "totalPrice": {"currencyCode": currency, "value": value},
        "description": "PRIVATE-PERSON-NAME-OR-TOKEN",
        "reductions": [{"url": "https://example.test/private-link"}],
    }


class QuoteBreakdownTests(unittest.TestCase):
    def test_multi_line_installation_and_promotions_are_aggregated(self):
        quote = {"details": [
            line("INSTALLATION", "7.50"),
            line("INSTALLATION", "0.00"),
            line("DURATION", "40.00"),
            line("GIFT", "0.00"),
            line("OTHER", "3.00"),
        ]}
        result = breakdown.inspect_checkout_details(quote)
        self.assertEqual(result.status, "LINES_PRESENT")
        self.assertEqual(result.installation_lines, 2)
        self.assertEqual(str(result.installation_listed_eur), "7.50")
        self.assertEqual(str(result.duration_listed_eur), "40.00")
        self.assertEqual((result.duration_lines, result.promotion_lines, result.other_lines), (1, 1, 1))
        self.assertIn("tax basis unknown", result.summary())
        self.assertNotIn("PRIVATE-PERSON", result.summary())
        self.assertNotIn("private-link", result.summary())

    def test_missing_zero_and_too_many_details_do_not_imply_zero_fees(self):
        for quote in ({}, {"details": []}, {"details": [line("DURATION", "1")] * 121}):
            with self.subTest(quote=quote):
                result = breakdown.inspect_checkout_details(quote)
                self.assertEqual(result.status, "UNVERIFIED")
                self.assertIn("UNVERIFIED", result.summary())
        with_zero = breakdown.inspect_checkout_details({"details": [line("INSTALLATION", "0")]})
        self.assertIn("does not guarantee", with_zero.summary())

    def test_unknown_detail_categories_are_not_misread_as_setup(self):
        result = breakdown.inspect_checkout_details({"details": [line("NEW_CHARGE", "6.00")]})
        self.assertEqual(result.installation_lines, 0)
        self.assertEqual(result.other_lines, 1)
        self.assertIn("unclassified", result.summary())

    def test_invalid_amounts_and_currency_fail_as_unverified(self):
        cases = [
            [line("INSTALLATION", "9.00", "USD")],
            [line("INSTALLATION", "-5.00")],
            [line("INSTALLATION", "1.234")],
            [{"detailType": "INSTALLATION", "totalPrice": "5 EUR"}],
            [line("DURATION", "2.00"), None],
            [{"detailType": 3, "totalPrice": {"currencyCode": "EUR", "value": 1}}],
        ]
        for case in cases:
            with self.subTest(case=case):
                result = breakdown.inspect_checkout_details({"details": case})
                self.assertEqual(result.status, "UNVERIFIED")
                self.assertNotIn("PRIVATE-PERSON", result.summary())

    def test_commercial_report_includes_itemized_evidence_checklist(self):
        policy = PurchasePolicy.from_env({
            "OVH_AUTO_BUY_ENABLED": "false",
            "OVH_AUTO_BUY_MAX_TOTAL_EUR": "80.00",
            "OVH_AUTO_BUY_MAX_MONTHLY_EUR": "37.00",
        })
        choice = Stock(APPROVED_FQNS[0], STORAGE_PRIORITIES[0], "bhs", "1H-high")
        quote_data = {"prices": {
            "withoutTax": {"currencyCode": "EUR", "value": "40.00"},
            "tax": {"currencyCode": "EUR", "value": "9.20"},
            "withTax": {"currencyCode": "EUR", "value": "49.20"},
        }, "commitmentDuration": "P0M"}
        quote_data["details"] = [line("INSTALLATION", "6.00"), line("DURATION", "40.00")]

        def fake_cart(_client, *, quote_validator, renewal_observer, **kwargs):
            quote_validator(quote_data)
            renewal_observer(({"duration": "P1M", "prices": [{
                "label": "RENEW", "price": {"currencyCode": "EUR", "value": "25.00"},
            }]},))

        with tempfile.TemporaryDirectory() as location:
            root = Path(location)
            with patch.object(audit, "verify_available_test_fqn"):
                with patch.object(audit, "test_order", side_effect=fake_cart):
                    report = audit.audit_commercial(
                        policy, stocks=[choice], client=object(),
                        journal=OrderJournal(root / "intent.json"),
                        quota=PurchaseQuota(root / "quota.json"),
                        pending_check=lambda _c, _max: PendingOrderInventory(
                            AccountInventory(0, 15, 2), 89, 0, 0, 22,
                        ),
                        payment_check=lambda _c: PaymentEvidence("VALID_DEFAULT"),
                    )
        self.assertIn("INITIAL CART DETAIL LINES", report)
        self.assertIn("listed amount 6.00 EUR", report)
        self.assertIn("EVIDENCE CHECKLIST (NO PURCHASE)", report)
        self.assertIn("INITIAL_VAT=WITHIN_LIMIT(49.20/80.00)", report)
        self.assertIn("SETUP_DETAILS=PRESENT_TAX_UNVERIFIED", report)
        self.assertIn("MONTHLY_VAT=UNVERIFIED", report)
        self.assertIn("CONTRACT_TERMS=UNVERIFIED", report)
        self.assertIn("FINAL_DECISION=BLOCKED", report)
        self.assertIn("AUTO-BUY BLOCKED", report)
        self.assertNotIn("PRIVATE-PERSON", report)
        self.assertNotIn("private-link", report)
        self.assertLess(len(report), 8000)


if __name__ == "__main__":
    unittest.main()
