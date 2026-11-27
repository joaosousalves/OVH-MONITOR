"""Offline integration of the real two-slot ledger and concurrent POST gate."""
import json
import os
from decimal import Decimal
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import live_batch as batch
from account_reconciliation import AccountInventory
from ksle_monitor import Stock
from pending_orders import PendingOrderInventory
from purchase_guard import APPROVED_FQNS, OrderJournal, PurchasePolicy, PurchaseSafetyError
from purchase_quota import PurchaseQuota
from retained_cart import persist_retained


class LiveBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.quota = PurchaseQuota(self.directory / "purchases-v2.json")
        self.journal = OrderJournal(self.directory / "legacy.json")
        self.policy = PurchasePolicy.from_env({
            "OVH_AUTO_BUY_MAX_TOTAL_EUR": "90.00",
            "OVH_AUTO_BUY_MAX_MONTHLY_EUR": "37.00",
        })
        self.stock = Stock(APPROVED_FQNS[0], APPROVED_FQNS[0].split(".")[2], "bhs", "1H-low")
        self.client = Mock()
        self.client.request.side_effect = lambda method, path: {
            "cartId": path.split("/")[-1], "expire": "2030-01-01T00:00:00Z"
        }
        self.env = patch.dict(os.environ, {"OVH_LIVE_CHECKOUT_ENABLED": "true"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.remote = PendingOrderInventory(AccountInventory(0, 0, 2), 0, 0, 0)
        self.submissions = []
        self.client._submit_live_checkout.side_effect = self.submit

    def submit(self, cart_id):
        self.submissions.append(cart_id)
        return {"orderId": 100 if cart_id == "cart-a" else 200}

    def prepare(self, policy, client, stocks, journal, quota, path, **kwargs):
        self.assertTrue(kwargs["allow_submitted"])
        cid = "cart-b" if path.stem.endswith("-2") else "cart-a"
        persist_retained(path, cart_id=cid, item_id=1, fqn=stocks[0].fqn,
                         datacenter=stocks[0].datacenter, expire="2030-01-01T00:00:00Z")

    def run_batch(self):
        with patch.object(batch, "prepare_live_cart", side_effect=self.prepare), \
             patch.object(batch, "verify_retained", return_value=Decimal("66.40")), \
             patch.object(batch, "reconcile_pending_orders", return_value=self.remote):
            return batch.run_live_batch(self.policy, [self.stock], self.journal, self.quota,
                                        self.directory, lambda: self.client)

    def test_both_slots_persisted_before_concurrent_posts_and_no_third(self):
        barrier = threading.Barrier(2, timeout=3)
        def submit(cid):
            self.assertEqual([r["status"] for r in self.quota.read()], ["reserved", "reserved"])
            barrier.wait()
            return self.submit(cid)
        self.client._submit_live_checkout.side_effect = submit
        result = self.run_batch()
        self.assertEqual(result.count("LIVE CHECKOUT SUBMITTED:"), 2)
        self.assertEqual(set(self.submissions), {"cart-a", "cart-b"})
        raw = json.loads(self.quota.path.read_text())
        self.assertEqual(raw["submittedCount"], 2)
        self.assertEqual(raw["reservedCount"], 2)
        self.assertEqual(raw["confirmedCount"], 0)
        self.assertIn("quota reached", self.run_batch())
        self.assertEqual(len(self.submissions), 2)

    def test_one_unknown_does_not_erase_other_success_and_blocks_retries(self):
        def submit(cid):
            if cid == "cart-b":
                raise TimeoutError()
            return self.submit(cid)
        self.client._submit_live_checkout.side_effect = submit
        result = self.run_batch()
        self.assertIn("LIVE CHECKOUT SUBMITTED", result)
        self.assertIn("LIVE CHECKOUT UNKNOWN", result)
        self.assertEqual(self.quota.counts(self.quota.read()),
                         {"reservedCount": 2, "submittedCount": 1, "confirmedCount": 0})
        self.assertIn("uncertain earlier", self.run_batch())
        self.assertEqual(self.client._submit_live_checkout.call_count, 2)

    def test_prior_acknowledged_order_does_not_require_payment_or_delivery(self):
        slot = self.quota.reserve(fqn=self.stock.fqn, datacenter="bhs",
                                  total=Decimal("66.40"), cart_id="old-cart")
        self.quota.update_outcome(slot=slot, status="submitted", order_id=300)
        self.remote = PendingOrderInventory(AccountInventory(0, 0, 2), 1, 1, 1,
                                            target_pending_ids=(300,))
        result = self.run_batch()
        self.assertEqual(result.count("LIVE CHECKOUT SUBMITTED:"), 1)
        self.assertEqual(self.quota.counts(self.quota.read())["submittedCount"], 2)

    def test_unseen_prior_order_still_counts_against_remote_quota(self):
        entries = [{"orderId": 300, "status": "submitted"}]
        self.assertEqual(batch.effective_usage(entries, self.remote), 1)
        remote = PendingOrderInventory(AccountInventory(0, 0, 2), 1, 1, 1,
                                       target_pending_ids=(999,))
        self.assertEqual(batch.effective_usage(entries, remote), 2)

    def test_only_validated_cart_is_submitted(self):
        def verify(client, record, path):
            if record["cart_id"] == "cart-b":
                raise PurchaseSafetyError("invalid quote")
            return Decimal("66.40")
        with patch.object(batch, "prepare_live_cart", side_effect=self.prepare), \
             patch.object(batch, "verify_retained", side_effect=verify), \
             patch.object(batch, "reconcile_pending_orders", return_value=self.remote):
            result = batch.run_live_batch(self.policy, [self.stock], self.journal, self.quota,
                                          self.directory, lambda: self.client)
        self.assertEqual(result.count("LIVE CHECKOUT SUBMITTED:"), 1)
        self.assertEqual(self.quota.counts(self.quota.read())["submittedCount"], 1)
        self.assertEqual(self.submissions, ["cart-a"])

    def test_remote_existing_service_limits_batch_to_one(self):
        self.remote = PendingOrderInventory(AccountInventory(1, 1, 2), 0, 0, 0)
        self.assertEqual(self.run_batch().count("LIVE CHECKOUT SUBMITTED:"), 1)
        self.assertEqual(self.quota.counts(self.quota.read())["submittedCount"], 1)

    def test_no_stock_no_api_calls(self):
        result = batch.run_live_batch(self.policy, [], self.journal, self.quota,
                                      self.directory, lambda: self.client)
        self.assertIn("WAITING", result)
        self.client.request.assert_not_called()
        self.client._submit_live_checkout.assert_not_called()


if __name__ == "__main__":
    unittest.main()
