"""Real purchase gates exercised ONLY against a fake checkout and temp quota."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ksle_monitor as monitor
import offline_purchase_flow as flow
from purchase_guard import PurchaseSafetyError
from purchase_quota import PurchaseQuota


class OfflinePurchaseFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.quota = PurchaseQuota(Path(self.tmp.name) / "quota.json")
        self.policy = flow._policy()
        self.stock = flow._stock(0)
        self.proof = flow.fake_verified_evidence(self.stock.storage)

    def attempt(self, proof=None, stocks=None, gateway=None):
        return flow.attempt_fake_purchase(
            self.policy, [self.stock] if stocks is None else stocks,
            self.proof if proof is None else proof, self.quota,
            flow.FakeCheckoutGateway() if gateway is None else gateway,
        )

    def test_complete_offline_flow(self):
        output = flow.simulate_safe_purchase_flow()
        self.assertEqual(len(output), 7)
        self.assertIn("SIMULATION ONLY", output[0])
        self.assertIn("mixed disks 2/2", output[2])
        self.assertIn("Two identical", output[4])
        self.assertIn("Timeout persists UNKNOWN", output[5])
        self.assertIn("no OVH request", output[-1])

    def test_reject_nonfake_gateway_before_reservation(self):
        class RealishGateway:
            def submit(self, payload):
                raise AssertionError("Never use an actual checkout adapter")
        with self.assertRaisesRegex(PurchaseSafetyError, "Only offline"):
            self.attempt(gateway=RealishGateway())
        self.assertEqual(self.quota.read(), [])

    def test_refuse_live_policy_without_changing_ledger(self):
        with self.assertRaisesRegex(PurchaseSafetyError, "Only offline"):
            flow.attempt_fake_purchase(
                replace(self.policy, enabled=True), [self.stock], self.proof,
                self.quota, flow.FakeCheckoutGateway(),
            )
        self.assertEqual(self.quota.read(), [])

    def test_no_stock_or_unapproved_region_or_model_blocks(self):
        bad_model = monitor.Stock(
            self.stock.fqn.replace("ram-128g", "ram-64g"),
            self.stock.storage, "bhs", "1H-low",
        )
        unavailable = monitor.Stock(
            self.stock.fqn, self.stock.storage, "bhs", "unavailable",
        )
        outside_region = monitor.Stock(
            self.stock.fqn, self.stock.storage, "sgp", "1H-low",
        )
        for stocks in ([], [unavailable], [outside_region], [bad_model]):
            with self.subTest(stocks=stocks):
                with self.assertRaises(PurchaseSafetyError):
                    self.attempt(stocks=stocks)
        self.assertEqual(self.quota.read(), [])

    def test_unknown_price_or_contract_or_payment_blocks_before_reserve(self):
        for proof in (
            replace(self.proof, monthly_renewal_verified=False),
            replace(self.proof, monthly_including_vat=None),
            replace(self.proof, no_fixed_commitment_verified=False),
            replace(self.proof, payment_method_ready=False),
            replace(self.proof, remote_inventory_verified=False),
        ):
            with self.subTest(proof=proof):
                with self.assertRaisesRegex(PurchaseSafetyError, "SIMULATION BLOCKED"):
                    self.attempt(proof=proof)
        self.assertEqual(self.quota.read(), [])

    def test_confirmed_price_increase_inside_absolute_caps_is_blocked(self):
        for proof in (
            replace(self.proof, initial_including_vat=Decimal("67.00")),
            replace(self.proof, monthly_including_vat=Decimal("33.21")),
        ):
            with self.subTest(proof=proof):
                with self.assertRaisesRegex(PurchaseSafetyError, "owner-confirmed"):
                    self.attempt(proof=proof)
        self.assertEqual(self.quota.read(), [])

    def test_cross_configuration_or_cross_datacenter_quote_reuse_blocked(self):
        for proof in (
            flow.fake_verified_evidence(flow.STORAGE_PRIORITIES[1]),
            flow.fake_verified_evidence(self.stock.storage, dc="fra"),
            replace(self.proof, quote_identity_verified=False),
        ):
            with self.subTest(proof=proof):
                with self.assertRaisesRegex(PurchaseSafetyError, "not bound to exact FQN"):
                    self.attempt(proof=proof)
        self.assertEqual(self.quota.read(), [])

    def test_budget_caps_block_with_complete_but_overpriced_fake_quote(self):
        for proof in (
            replace(self.proof, monthly_including_vat=Decimal("38.00")),
            replace(self.proof, initial_including_vat=Decimal("81.00")),
        ):
            with self.subTest(proof=proof):
                with self.assertRaises(PurchaseSafetyError):
                    self.attempt(proof=proof)
        self.assertEqual(self.quota.read(), [])

    def test_timeout_and_rejection_are_persistent_unknown(self):
        for scenario in ("timeout", "rejected"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as root:
                q = PurchaseQuota(Path(root) / "quota.json")
                fake = flow.FakeCheckoutGateway(scenario)
                result = flow.attempt_fake_purchase(
                    self.policy, [self.stock], self.proof, q, fake,
                )
                self.assertEqual(result.status, "unknown")
                self.assertEqual(fake.calls, 1)
                self.assertEqual(PurchaseQuota(q.path).read()[0]["status"], "unknown")
                retry = flow.FakeCheckoutGateway()
                with self.assertRaises(PurchaseSafetyError):
                    flow.attempt_fake_purchase(
                        self.policy, [self.stock], self.proof, PurchaseQuota(q.path), retry,
                    )
                self.assertEqual(retry.calls, 0)

    def test_submitted_blocks_second_until_verified(self):
        fake = flow.FakeCheckoutGateway(order_id=1001)
        first = self.attempt(gateway=fake)
        self.assertEqual(first.status, "submitted")
        with self.assertRaises(PurchaseSafetyError):
            self.attempt()
        self.quota.update_outcome(slot=1, status="confirmed", order_id=1001)
        second = self.attempt(
            proof=flow.fake_verified_evidence(self.stock.storage, used=1),
            gateway=flow.FakeCheckoutGateway(order_id=1002),
        )
        self.assertEqual(second.slot, 2)

    def test_stale_remote_count_after_first_confirmation_blocks(self):
        self.attempt(gateway=flow.FakeCheckoutGateway(order_id=1001))
        self.quota.update_outcome(slot=1, status="confirmed", order_id=1001)
        with self.assertRaisesRegex(PurchaseSafetyError, "stale remote count"):
            self.attempt()
        self.assertEqual(len(self.quota.read()), 1)

    def test_runner_never_accesses_network_or_smtp(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            with patch.object(monitor, "send_mail", side_effect=AssertionError("smtp")):
                result = monitor.main(["--simulate-safe-purchase"])
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
