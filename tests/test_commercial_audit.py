"""No live cart/checkout: all Phase I commercial audit behavior uses fakes."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import commercial_audit as audit
import ksle_monitor as monitor
from account_reconciliation import AccountInventory
from order_probe import OVHCartClient, OrderProbeError
from pending_orders import PendingOrderInventory, PendingOrdersError
from purchase_guard import APPROVED_FQNS, OrderJournal, PurchasePolicy, PurchaseSafetyError
from purchase_quota import PurchaseQuota


def policy() -> PurchasePolicy:
    return PurchasePolicy.from_env({
        "OVH_AUTO_BUY_ENABLED": "false",
        "OVH_AUTO_BUY_MAX_TOTAL_EUR": "80.00",
        "OVH_AUTO_BUY_MAX_MONTHLY_EUR": "37.00",
    })


def stock(status="1H-high", index=0) -> monitor.Stock:
    from purchase_guard import STORAGE_PRIORITIES
    return monitor.Stock(
        fqn=APPROVED_FQNS[index], storage=STORAGE_PRIORITIES[index],
        datacenter="bhs", status=status,
    )


def quote(net="40.00", vat="9.20", gross="49.20", commitment=None):
    response = {"prices": {
        "withoutTax": {"value": net, "currencyCode": "EUR"},
        "tax": {"value": vat, "currencyCode": "EUR"},
        "withTax": {"value": gross, "currencyCode": "EUR"},
    }}
    if commitment is not None:
        response["commitmentDuration"] = commitment
    return response


def item(renew="25.00", *, gross=None, commitment=None):
    row = {"label": "RENEW", "price": {"currencyCode": "EUR", "value": renew}}
    if gross is not None:
        row["priceWithTax"] = {"currencyCode": "EUR", "value": gross}
    obj = {"duration": "P1M", "prices": [row]}
    if commitment is not None:
        obj["engagementMonths"] = commitment
    return obj


def inventory(services=0, pending=0, max_servers=2):
    return PendingOrderInventory(
        AccountInventory(services, 15, max_servers), 89, 0 if pending == 0 else 1,
        pending, 22,
    )


def fake_cart(quote_data=None, items=None):
    if quote_data is None:
        quote_data = quote(commitment="P0M")
    if items is None:
        items = (item(), item(renew="1.00"))

    def mock(_client, *, quote_validator, renewal_observer, **_kwargs):
        quote_validator(quote_data)
        renewal_observer(tuple(items))
        return "Temporary cart removed (offline fake)"
    return mock


class CommercialAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.journal = OrderJournal(root / "intent.json")
        self.quota = PurchaseQuota(root / "purchases-v2.json")

    def run_audit(self, *, stocks=None, pending_check=None, client=None):
        return audit.audit_commercial(
            policy(), stocks=stocks if stocks is not None else [stock()],
            client=client if client is not None else object(),
            journal=self.journal, quota=self.quota,
            pending_check=pending_check or (lambda _client, _limit: inventory()),
        )

    def test_no_stock_skips_account_and_cart(self):
        account = Mock(side_effect=AssertionError("Unexpected account call"))
        with patch.object(audit, "test_order") as cart:
            result = self.run_audit(
                stocks=[stock(status="unavailable")], pending_check=account, client=None,
            )
        self.assertIn("WAITING", result)
        account.assert_not_called()
        cart.assert_not_called()

    def test_eligible_stock_reports_initial_vat_and_unverified_monthly(self):
        with patch.object(audit, "verify_available_test_fqn"):
            with patch.object(audit, "test_order", side_effect=fake_cart()) as cart:
                report = self.run_audit()
        self.assertIn("initial net 40.00 EUR + VAT 9.20 EUR = 49.20 EUR", report)
        self.assertIn("MONTHLY WITH VAT UNVERIFIED", report)
        self.assertIn("Separate installation fee NOT itemized/verified", report)
        self.assertIn("Zero-commitment indicator observed, but legal terms NOT VERIFIED", report)
        self.assertIn("AUTO-BUY BLOCKED (NO PURCHASE)", report)
        self.assertIn("final monthly renewal including VAT not verified", report)
        self.assertIn("contractual freedom from 12/24-month commitment not verified", report)
        self.assertIn("eligible payment method not verified", report)
        self.assertIn("0/2", report)
        self.assertIn("Checkout/payment remain DISABLED", report)
        self.assertEqual(self.quota.read(), [])
        self.assertFalse(self.journal.path.exists())
        self.assertEqual(cart.call_count, 1)

    def test_quote_vat_mismatch_fails_closed(self):
        for net, vat, gross in (("40.00", "9.19", "49.20"), ("40.00", "9.21", "49.20")):
            with self.subTest(net=net, vat=vat, gross=gross):
                with self.assertRaisesRegex(PurchaseSafetyError, "does not reconcile"):
                    audit.validate_initial_price(quote(net, vat, gross), Decimal("80"))

    def test_quote_bad_vat_or_currency_fails_closed(self):
        cases = [
            {"prices": {"withTax": {"currencyCode": "EUR", "value": "49.20"}}},
            {"prices": {"withTax": {"currencyCode": "EUR", "value": "49.20"},
                        "withoutTax": {"currencyCode": "USD", "value": "40.00"},
                        "tax": {"currencyCode": "EUR", "value": "9.20"}}},
            quote(net="40.005"), quote(vat="-1.00"),
        ]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(PurchaseSafetyError):
                    audit.validate_initial_price(value, Decimal("80"))

    def test_initial_ceiling_is_enforced_before_renewals(self):
        with patch.object(audit, "verify_available_test_fqn"):
            with patch.object(audit, "test_order", side_effect=fake_cart(quote("70.00", "16.10", "86.10"))):
                with self.assertRaisesRegex(PurchaseSafetyError, "PRICE BLOCKED"):
                    self.run_audit()

    def test_missing_gross_renewal_tax_does_not_assume_23_percent(self):
        result, reason = audit._renewal_vat_gross((item(), item()))
        self.assertIsNone(result)
        self.assertIn("not explicitly provided", reason)

    def test_structured_monthly_gross_under_cap_remains_partial(self):
        with patch.object(audit, "verify_available_test_fqn"):
            with patch.object(audit, "test_order", side_effect=fake_cart(items=(
                item(renew="20.00", gross="25.00"), item(renew="3.00", gross="4.00"),
            ))):
                report = self.run_audit()
        self.assertIn("gross RENEW evidence 29.00 EUR <= 37.00 EUR", report)
        self.assertIn("final monthly invoice and future discounts NOT VERIFIED", report)
        self.assertIn("COMMERCIAL AUDIT PARTIAL", report)

    def test_monthly_gross_above_cap_warns_without_authorizing(self):
        with patch.object(audit, "verify_available_test_fqn"):
            with patch.object(audit, "test_order", side_effect=fake_cart(items=(
                item(renew="34.00", gross="41.00"),
            ))):
                report = self.run_audit()
        self.assertIn("MONTHLY PRICE WARNING", report)
        self.assertIn("exceeds ceiling 37.00 EUR", report)
        self.assertIn("Checkout/payment remain DISABLED", report)

    def test_gross_renewal_does_not_pass_with_missing_base_price(self):
        broken = {"duration": "P1M", "prices": [{
            "label": "RENEW", "priceWithTax": {"currencyCode": "EUR", "value": "29.00"},
        }]}
        with patch.object(audit, "verify_available_test_fqn"):
            with patch.object(audit, "test_order", side_effect=fake_cart(items=(broken,))):
                report = self.run_audit()
        self.assertIn("MONTHLY WITH VAT UNVERIFIED: incomplete RENEW", report)
        self.assertNotIn("gross RENEW evidence 29.00", report)

    def test_gross_renewal_lower_than_base_price_is_unverified(self):
        with patch.object(audit, "verify_available_test_fqn"):
            with patch.object(audit, "test_order", side_effect=fake_cart(items=(
                item(renew="35.00", gross="30.00"),
            ))):
                report = self.run_audit()
        self.assertIn("inconsistent evidence", report)
        self.assertNotIn("gross RENEW evidence 30.00", report)

    def test_fixed_commitment_rejected_in_quote_and_addon(self):
        with patch.object(audit, "verify_available_test_fqn"):
            for quote_data, items in (
                (quote(commitment="P12M"), (item(),)),
                (quote(), (item(commitment=24),)),
            ):
                with self.subTest(quote_data=quote_data, items=items):
                    with patch.object(audit, "test_order", side_effect=fake_cart(quote_data, items)):
                        with self.assertRaisesRegex(PurchaseSafetyError, "Fixed-term commitment"):
                            self.run_audit()

    def test_remote_cap_and_overlap_block_before_cart(self):
        for services, pending in ((2, 0), (0, 2), (1, 1)):
            with self.subTest(services=services, pending=pending):
                with patch.object(audit, "test_order") as cart:
                    if services == 1:
                        with self.assertRaisesRegex(PurchaseSafetyError, "overlap unresolved"):
                            self.run_audit(pending_check=lambda _c, _limit: inventory(services, pending))
                    else:
                        report = self.run_audit(
                            pending_check=lambda _c, _limit: inventory(services, pending),
                        )
                        self.assertIn("LIMIT REACHED", report)
                    cart.assert_not_called()

    def test_missing_remote_order_access_fails_closed_before_cart(self):
        with patch.object(audit, "test_order") as cart:
            with self.assertRaisesRegex(PendingOrdersError, "inconclusive"):
                self.run_audit(pending_check=Mock(side_effect=PendingOrdersError("inconclusive")))
            cart.assert_not_called()

    def test_existing_local_intent_blocks_before_remote_requests(self):
        with patch.object(self.journal, "read", return_value={"status": "unknown"}):
            remote = Mock()
            with self.assertRaisesRegex(PurchaseSafetyError, "intent unresolved"):
                self.run_audit(pending_check=remote)
            remote.assert_not_called()

    def test_recheck_local_quota_after_temporary_quote(self):
        reads = iter([[], [{"status": "reserved"}]])
        with patch.object(self.quota, "read", side_effect=lambda: next(reads)):
            with patch.object(audit, "verify_available_test_fqn"):
                with patch.object(audit, "test_order", side_effect=fake_cart()):
                    with self.assertRaisesRegex(PurchaseSafetyError, "Unresolved local"):
                        self.run_audit()

    def test_unsupported_checkout_mutations_remain_blocked(self):
        client = OVHCartClient("offline-test-token")
        with self.assertRaisesRegex(OrderProbeError, "Blocked unsupported"):
            client.request("POST", "/order/cart/abc-123/checkout", {})
        with self.assertRaisesRegex(OrderProbeError, "Blocked unsupported"):
            client.request("POST", "/me/order/123/pay", {})

    def test_cli_does_not_send_email(self):
        with patch.object(audit, "run_commercial_audit", return_value=0) as fn:
            with patch.object(monitor, "send_mail") as email:
                self.assertEqual(monitor.main(["--commercial-audit"]), 0)
        fn.assert_called_once()
        email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
