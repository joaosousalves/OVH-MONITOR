"""Exhaustive offline checkout simulator and persistent reconciliation tests."""
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import checkout_simulator as sim
import purchase_guard as guard


POLICY_ENV = {
    "OVH_AUTO_BUY_ENABLED": "false",
    "OVH_AUTO_BUY_MAX_TOTAL_EUR": "45.00",
    "OVH_AUTO_BUY_MAX_MONTHLY_EUR": "37.00",
    "OVH_AUTO_BUY_ALLOWED_DATACENTERS": "bhs,gra",
}


def policy():
    return guard.PurchasePolicy.from_env(POLICY_ENV)


class FakeOrderClient:
    def __init__(self, payload="inProgress"):
        self.payload = payload
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        return self.payload


class CheckoutSimulationTests(unittest.TestCase):
    def test_offline_accepted_checkout_submits_exactly_once(self):
        output = sim.simulate_checkout("accepted", policy())
        self.assertIn("SIMULATION ONLY", output)
        self.assertIn("status=submitted", output)
        self.assertIn("repeat blocked", output)
        self.assertIn("no real order", output)

    def test_offline_timeout_is_uncertain_and_never_retried(self):
        output = sim.simulate_checkout("timeout", policy())
        self.assertIn("status=unknown", output)
        self.assertIn("repeat blocked", output)

    def test_offline_rejection_is_uncertain_and_never_retried(self):
        output = sim.simulate_checkout("rejected", policy())
        self.assertIn("status=unknown", output)
        self.assertIn("repeat blocked", output)

    def test_offline_simulation_above_price_limit_refused(self):
        with self.assertRaises(sim.SimulationError):
            sim.simulate_checkout("accepted", policy(), example_amount=Decimal("45.01"))

    def test_simulation_never_uses_network_or_live_api(self):
        with patch.object(sim, "OVHCartClient") as client:
            with patch.object(sim, "FakeGateway", wraps=sim.FakeGateway) as fake:
                sim.simulate_checkout("accepted", policy())
            client.assert_not_called()
            fake.assert_called_once()

    def test_simulator_refuses_active_purchase_setting(self):
        bad = guard.PurchasePolicy(enabled=True, max_total_eur=Decimal("45"),
                                   datacenters=("bhs",))
        with self.assertRaises(sim.SimulationError):
            sim.simulate_checkout("accepted", bad)

    def test_default_gateway_validates_payload(self):
        fake = sim.FakeGateway("accepted")
        with self.assertRaises(sim.SimulationError):
            fake.submit({"autoPayWithPreferredPaymentMethod": False,
                         "waiveRetractationPeriod": False})
        with self.assertRaises(sim.SimulationError):
            fake.submit({"autoPayWithPreferredPaymentMethod": True,
                         "waiveRetractationPeriod": True})

    def test_submitted_journal_persists_order_id_and_refuses_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = guard.OrderJournal(Path(directory) / "intent.json")
            self.assertTrue(ledger.reserve_once(
                fqn=guard.EXPECTED_FQN, datacenter="bhs", total=Decimal("39.99")
            ))
            ledger.record_outcome("submitted", order_id=123456789)
            self.assertEqual(guard.OrderJournal(ledger.path).read()["orderId"], 123456789)
            self.assertFalse(ledger.reserve_once(
                fqn=guard.EXPECTED_FQN, datacenter="bhs", total=Decimal("39.99")
            ))
            with self.assertRaises(guard.PurchaseSafetyError):
                ledger.record_outcome("submitted", order_id=123456789)

    def test_unknown_journal_persists_and_refuses_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = guard.OrderJournal(Path(directory) / "intent.json")
            ledger.reserve_once(fqn=guard.EXPECTED_FQN, datacenter="bhs",
                                total=Decimal("39.99"))
            ledger.record_outcome("unknown")
            self.assertEqual(ledger.read()["status"], "unknown")
            self.assertFalse(ledger.reserve_once(
                fqn=guard.EXPECTED_FQN, datacenter="gra", total=Decimal("39.99")
            ))
            with self.assertRaises(guard.PurchaseSafetyError):
                ledger.record_outcome("submitted", order_id=1234)

    def test_invalid_status_change_cannot_reset_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = guard.OrderJournal(Path(directory) / "intent.json")
            ledger.reserve_once(fqn=guard.EXPECTED_FQN, datacenter="bhs",
                                total=Decimal("39.99"))
            for status, order_id in (("paid", None), ("submitted", None),
                                     ("submitted", "123"), ("unknown", 100)):
                with self.subTest(status=status):
                    with self.assertRaises(guard.PurchaseSafetyError):
                        ledger.record_outcome(status, order_id=order_id)
            self.assertEqual(ledger.read()["status"], "reserved")

    def test_known_order_status_get_does_not_unlock_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = guard.OrderJournal(Path(directory) / "intent.json")
            ledger.reserve_once(fqn=guard.EXPECTED_FQN, datacenter="bhs",
                                total=Decimal("39.99"))
            ledger.record_outcome("submitted", order_id=123456789)
            fake = FakeOrderClient(payload="inProgress")
            result = sim.read_only_order_status(fake, ledger)
            self.assertIn("inProgress", result)
            self.assertIn("journal remains locked", result)
            self.assertEqual(fake.calls, [("GET", "/me/order/123456789/status")])
            self.assertEqual(ledger.read()["status"], "submitted")

    def test_unknown_order_id_requires_manual_review_no_api_request(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = guard.OrderJournal(Path(directory) / "intent.json")
            ledger.reserve_once(fqn=guard.EXPECTED_FQN, datacenter="bhs",
                                total=Decimal("39.99"))
            ledger.record_outcome("unknown")
            fake = FakeOrderClient()
            result = sim.read_only_order_status(fake, ledger)
            self.assertIn("do not retry", result)
            self.assertFalse(fake.calls)

    def test_malformed_remote_order_status_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = guard.OrderJournal(Path(directory) / "intent.json")
            ledger.reserve_once(fqn=guard.EXPECTED_FQN, datacenter="bhs",
                                total=Decimal("39.99"))
            ledger.record_outcome("submitted", order_id=123456789)
            for payload in (None, "", {"unexpected": True}, {"status": []}):
                with self.subTest(payload=payload):
                    with self.assertRaises(sim.SimulationError):
                        sim.read_only_order_status(FakeOrderClient(payload), ledger)

    def test_notification_is_explicitly_simulated(self):
        subject, body = sim.notification_text(status="unknown", datacenter="bhs",
                                              amount=Decimal("39.99"))
        self.assertIn("[SIMULATION ONLY]", subject)
        self.assertIn("no OVHcloud order", body)
        self.assertIn("NOT KNOWN", body)

    def test_payment_not_emitted_during_offline_sim(self):
        with patch.object(sim, "notification_text", wraps=sim.notification_text) as notify:
            output = sim.simulate_checkout("accepted", policy())
        notify.assert_called_once()
        self.assertIn("no email dispatched", output)

    def test_simulation_cli_uses_configured_cap(self):
        with patch.dict(os.environ, POLICY_ENV):
            self.assertEqual(sim.run_checkout_simulation("accepted"), 0)
        with patch.dict(os.environ, {**POLICY_ENV, "OVH_AUTO_BUY_ENABLED": "true"}):
            self.assertEqual(sim.run_checkout_simulation("accepted"), 1)


if __name__ == "__main__":
    unittest.main()

