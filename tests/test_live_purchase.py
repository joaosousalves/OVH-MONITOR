import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

import live_purchase as live
from order_probe import OVHCartClient, OrderProbeError
from purchase_guard import PurchasePolicy, OrderJournal, APPROVED_FQNS
from purchase_quota import PurchaseQuota
from retained_cart import persist_retained


class LiveCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        self.cart = directory / "retained-cart.json"
        self.quota = PurchaseQuota(directory / "purchases-v2.json")
        self.journal = OrderJournal(directory / "legacy.json")
        self.policy = PurchasePolicy.from_env({
            "OVH_AUTO_BUY_MAX_TOTAL_EUR": "90.00",
            "OVH_AUTO_BUY_MAX_MONTHLY_EUR": "37.00",
        })
        persist_retained(self.cart, cart_id="cart-123", item_id=1,
                         fqn=APPROVED_FQNS[0], datacenter="bhs",
                         expire="2030-01-01T00:00:00Z")
        self.client = Mock()
        self.client.request.return_value = {"cartId": "cart-123", "expire": "2030-01-01T00:00:00Z"}
        self.client._submit_live_checkout.return_value = {"orderId": 12345}
        self.inventory = Mock(overlap_unverified=False, upper_bound=0)
        self.env = patch.dict(os.environ, {"OVH_LIVE_CHECKOUT_ENABLED": "true"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_checkout(self):
        return live.try_retained_checkout(self.policy, self.client, self.journal,
                                          self.quota, self.cart)

    def test_disabled_never_calls_transport(self):
        with patch.dict(os.environ, {"OVH_LIVE_CHECKOUT_ENABLED": "false"}):
            self.assertIsNone(self.run_checkout())
        self.client.request.assert_not_called()
        self.client._submit_live_checkout.assert_not_called()

    def test_missing_proof_prevents_reservation(self):
        from purchase_guard import PurchaseSafetyError
        with patch.object(live, "reconcile_pending_orders", return_value=self.inventory), \
             patch.object(live, "verify_retained", side_effect=PurchaseSafetyError("missing proof")):
            self.assertIn("BLOCKED", self.run_checkout())
        self.assertEqual(self.quota.read(), [])
        self.client._submit_live_checkout.assert_not_called()

    def test_durable_reservation_before_single_post_and_no_duplicate(self):
        def submit(cart_id):
            self.assertEqual(cart_id, "cart-123")
            self.assertEqual(self.quota.read()[0]["status"], "reserved")
            self.assertEqual(self.quota.read()[0]["cartId"], cart_id)
            return {"orderId": 12345}
        self.client._submit_live_checkout.side_effect = submit
        with patch.object(live, "reconcile_pending_orders", return_value=self.inventory), \
             patch.object(live, "verify_retained", return_value=Decimal("66.40")):
            self.assertIn("SUBMITTED", self.run_checkout())
            self.assertEqual(self.quota.read()[0]["status"], "submitted")
            self.assertIn("BLOCKED", self.run_checkout())
        self.client._submit_live_checkout.assert_called_once_with("cart-123")

    def test_timeout_is_persistent_unknown(self):
        self.client._submit_live_checkout.side_effect = TimeoutError()
        with patch.object(live, "reconcile_pending_orders", return_value=self.inventory), \
             patch.object(live, "verify_retained", return_value=Decimal("66.40")):
            self.assertIn("UNKNOWN", self.run_checkout())
            self.assertEqual(self.quota.read()[0]["status"], "unknown")
            self.assertIn("BLOCKED", self.run_checkout())
        self.client._submit_live_checkout.assert_called_once()

    def test_public_transport_stays_blocked_and_private_payload_exact(self):
        client = OVHCartClient("fake-token")
        with patch.object(client, "_request_validated") as transport:
            with self.assertRaises(OrderProbeError):
                client.request("POST", "/order/cart/cart-123/checkout", {})
            transport.assert_not_called()
            client._submit_live_checkout("cart-123")
            transport.assert_called_once_with("POST", "/order/cart/cart-123/checkout", {
                "autoPayWithPreferredPaymentMethod": True,
                "waiveRetractationPeriod": False,
            })

    def test_confirmed_cart_cannot_be_reused_after_crash(self):
        slot = self.quota.reserve(fqn=APPROVED_FQNS[0], datacenter="bhs",
                                  total=Decimal("66.40"), cart_id="cart-123")
        self.quota.update_outcome(slot=slot, status="submitted", order_id=12345)
        self.quota.update_outcome(slot=slot, status="confirmed", order_id=12345)
        self.assertIsNone(self.run_checkout())
        self.assertFalse(self.cart.exists())
        self.client._submit_live_checkout.assert_not_called()

    def test_contract_digest_changes_with_terms(self):
        a = [{"name": "Terms", "content": "one month"}]
        b = [{"name": "Terms", "content": "twelve months"}]
        self.assertNotEqual(live.contract_digest(a)[0], live.contract_digest(b)[0])


if __name__ == "__main__":
    unittest.main()
