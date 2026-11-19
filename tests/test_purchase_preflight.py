"""Manual purchase preflight tests: all API, SMTP and payments are mocked."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ksle_monitor as monitor
import purchase_guard as guard
import purchase_preflight as pf
from order_probe import OrderProbeError, OVHCartClient
from payment_readiness import PaymentReadinessError
from purchase_quota import PurchaseQuota


def policy() -> guard.PurchasePolicy:
    return guard.PurchasePolicy.from_env({
        "OVH_AUTO_BUY_ENABLED": "false",
        "OVH_AUTO_BUY_MAX_TOTAL_EUR": "45.00",
    })


def stock(storage_index: int = 0, status: str = "1H-high") -> monitor.Stock:
    return monitor.Stock(
        fqn=guard.APPROVED_FQNS[storage_index],
        storage=guard.STORAGE_PRIORITIES[storage_index],
        datacenter="bhs",
        status=status,
    )


def quote_with_total(value: str):
    def mock_cart(_client, *, quote_validator, **_kwargs):
        quote_validator({"prices": {
            "withTax": {"currencyCode": "EUR", "value": value},
        }})
        return "TEST CART (NO ORDER)"
    return mock_cart


class PurchasePreflightTests(unittest.TestCase):
    def paths(self, root: str):
        return (
            guard.OrderJournal(Path(root) / "intent.json"),
            PurchaseQuota(Path(root) / "purchases-v2.json"),
        )

    def test_no_stock_skips_cart_credentials_and_payment(self):
        with tempfile.TemporaryDirectory() as root:
            journal, quota = self.paths(root)
            payment = Mock()
            with patch.object(guard, "test_order") as cart:
                result = pf.preflight(
                    policy(), stocks=[stock(status="unavailable")],
                    client=None, journal=journal, quota=quota,
                    payment_check=payment,
                )
            self.assertIn("WAITING", result)
            self.assertIn("no order submitted", result)
            cart.assert_not_called()
            payment.assert_not_called()
            self.assertEqual(quota.read(), [])

    def test_valid_quote_and_payment_creates_no_order_or_reservation(self):
        with tempfile.TemporaryDirectory() as root:
            journal, quota = self.paths(root)
            payment = Mock(return_value="READ-ONLY: valid default payment method")
            with patch.object(guard, "verify_available_test_fqn"):
                with patch.object(guard, "test_order", side_effect=quote_with_total("42.00")) as cart:
                    result = pf.preflight(
                        policy(), stocks=[stock()], client=object(),
                        journal=journal, quota=quota,
                        payment_check=payment,
                    )
            cart.assert_called_once()
            payment.assert_called_once()
            self.assertIn("PREFLIGHT ONLY (NO PURCHASE)", result)
            self.assertIn("42.00 EUR", result)
            self.assertIn("2/2 local reservation slots remain", result)
            self.assertIn("already on the future checkout approval list", result)
            self.assertIn("recurring monthly cost", result)
            self.assertFalse(journal.path.exists())
            self.assertFalse(quota.path.exists())

    def test_alternative_disk_is_explicitly_not_purchase_approved(self):
        with tempfile.TemporaryDirectory() as root:
            journal, quota = self.paths(root)
            with patch.object(guard, "verify_available_test_fqn"):
                with patch.object(guard, "test_order", side_effect=quote_with_total("39.99")):
                    result = pf.preflight(
                        policy(), stocks=[stock(storage_index=1)], client=object(),
                        journal=journal, quota=quota,
                        payment_check=lambda _client: "READ-ONLY: valid",
                    )
            self.assertIn("REVIEW REQUIRED", result)
            self.assertIn("NOT authorized for future checkout", result)
            self.assertEqual(quota.read(), [])

    def test_price_above_cap_stops_before_payment_and_never_reserves(self):
        with tempfile.TemporaryDirectory() as root:
            journal, quota = self.paths(root)
            payment = Mock()
            with patch.object(guard, "verify_available_test_fqn"):
                with patch.object(guard, "test_order", side_effect=quote_with_total("45.01")):
                    with self.assertRaisesRegex(guard.PurchaseSafetyError, "PRICE BLOCKED"):
                        pf.preflight(
                            policy(), stocks=[stock()], client=object(),
                            journal=journal, quota=quota,
                            payment_check=payment,
                        )
            payment.assert_not_called()
            self.assertEqual(quota.read(), [])

    def test_payment_not_ready_blocks_without_reservation(self):
        with tempfile.TemporaryDirectory() as root:
            journal, quota = self.paths(root)
            with patch.object(guard, "verify_available_test_fqn"):
                with patch.object(guard, "test_order", side_effect=quote_with_total("40.00")):
                    with self.assertRaisesRegex(PaymentReadinessError, "No valid card"):
                        pf.preflight(
                            policy(), stocks=[stock()], client=object(),
                            journal=journal, quota=quota,
                            payment_check=Mock(side_effect=PaymentReadinessError("No valid card")),
                        )
            self.assertEqual(quota.read(), [])

    def test_two_slots_full_skips_cart_and_payment(self):
        with tempfile.TemporaryDirectory() as root:
            journal, quota = self.paths(root)
            for index in (1, 2):
                self.assertEqual(
                    quota.reserve(fqn=guard.EXPECTED_FQN, datacenter="bhs",
                                  total=Decimal("40.00")), index)
                quota.update_outcome(slot=index, status="submitted", order_id=100 + index)
                quota.update_outcome(slot=index, status="confirmed", order_id=100 + index)
            payment = Mock()
            with patch.object(guard, "test_order") as cart:
                result = pf.preflight(
                    policy(), stocks=[stock()], client=object(),
                    journal=journal, quota=quota, payment_check=payment)
            self.assertIn("LIMIT REACHED", result)
            cart.assert_not_called()
            payment.assert_not_called()
            self.assertEqual(len(quota.read()), 2)

    def test_one_confirmed_slot_means_only_one_remaining(self):
        with tempfile.TemporaryDirectory() as root:
            journal, quota = self.paths(root)
            quota.reserve(fqn=guard.EXPECTED_FQN, datacenter="bhs",
                          total=Decimal("40.00"))
            quota.update_outcome(slot=1, status="submitted", order_id=123)
            quota.update_outcome(slot=1, status="confirmed", order_id=123)
            with patch.object(guard, "verify_available_test_fqn"):
                with patch.object(guard, "test_order", side_effect=quote_with_total("42.00")):
                    result = pf.preflight(
                        policy(), stocks=[stock()], client=object(),
                        journal=journal, quota=quota,
                        payment_check=lambda _client: "READ-ONLY: valid")
            self.assertIn("1/2 local reservation slots remain", result)
            self.assertEqual(len(quota.read()), 1)

    def test_unresolved_previous_attempt_blocks_preflight(self):
        with tempfile.TemporaryDirectory() as root:
            journal, quota = self.paths(root)
            quota.reserve(fqn=guard.EXPECTED_FQN, datacenter="bhs",
                          total=Decimal("40.00"))
            with patch.object(guard, "test_order") as cart:
                with self.assertRaisesRegex(guard.PurchaseSafetyError, "reconciled"):
                    pf.preflight(
                        policy(), stocks=[stock()], client=object(),
                        journal=journal, quota=quota,
                        payment_check=Mock())
            cart.assert_not_called()

    def test_setting_live_checkout_enabled_is_rejected(self):
        unsafe = guard.PurchasePolicy(
            enabled=True, max_total_eur=Decimal("45.00"),
            datacenters=("bhs",))
        with tempfile.TemporaryDirectory() as root:
            journal, quota = self.paths(root)
            with self.assertRaisesRegex(guard.PurchaseSafetyError, "not authorized"):
                pf.preflight(
                    unsafe, stocks=[stock()], client=object(),
                    journal=journal, quota=quota,
                    payment_check=Mock())
            self.assertFalse(quota.path.exists())

    def test_checkout_post_remains_blocked_at_transport(self):
        client = OVHCartClient("fake-bearer")
        with self.assertRaisesRegex(OrderProbeError, "Blocked unsupported operation"):
            client.request("POST", "/order/cart/abcdef/checkout", {})

    def test_cli_is_manual_and_does_not_use_monitor_mail(self):
        with patch.object(pf, "run_purchase_preflight", return_value=0) as preflight:
            with patch.object(monitor, "send_mail") as send:
                self.assertEqual(monitor.main(["--purchase-preflight"]), 0)
        preflight.assert_called_once()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
