"""Fail-closed quota tests for at most two KS-LE purchases across plans."""
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
import os
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import purchase_quota as q
import purchase_guard as guard


FIRST = guard.EXPECTED_FQN
SECOND = "27skle05-v1.ram-128g-ddr4-ecc-2400.softraid-2x450nvme"
THIRD = "27skle03-v1.ram-32g-ddr4-ecc-2400.softraid-2x480ssd"
AMOUNT = Decimal("39.99")


def ledger(path, approved=(FIRST, SECOND), max_servers=2):
    return q.PurchaseQuota(path, approved_fqns=approved, max_servers=max_servers)


class PurchaseQuotaTests(unittest.TestCase):
    def test_max_two_even_if_third_model_approved_fails(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "purchases-v1.json"
            with self.assertRaises(guard.PurchaseSafetyError):
                ledger(path, approved=(FIRST, SECOND, THIRD))

    def test_two_distinct_approved_orders_but_never_third(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "purchases-v1.json"
            original = ledger(path)
            self.assertTrue(original.reserve(fqn=FIRST, datacenter="bhs", total=AMOUNT))
            original.update_outcome(plan_code="27skle06-v1", status="submitted", order_id=12345)
            # No second order until the first is confirmed by a separate, future
            # manual/reconciled process.
            with self.assertRaises(guard.PurchaseSafetyError):
                original.reserve(fqn=SECOND, datacenter="gra", total=AMOUNT)
            original.update_outcome(plan_code="27skle06-v1", status="confirmed", order_id=12345)
            fresh = ledger(path)
            self.assertTrue(fresh.reserve(fqn=SECOND, datacenter="gra", total=AMOUNT))
            fresh.update_outcome(plan_code="27skle05-v1", status="submitted", order_id=23456)
            fresh.update_outcome(plan_code="27skle05-v1", status="confirmed", order_id=23456)
            self.assertFalse(ledger(path).reserve(fqn=FIRST, datacenter="bhs", total=AMOUNT))
            self.assertFalse(ledger(path).reserve(fqn=SECOND, datacenter="gra", total=AMOUNT))
            self.assertEqual(len(ledger(path).read()), 2)
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_secondary_is_not_approved_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            obj = q.PurchaseQuota(Path(d) / "purchases-v1.json")
            with self.assertRaises(guard.PurchaseSafetyError):
                obj.reserve(fqn=SECOND, datacenter="bhs", total=AMOUNT)
            self.assertEqual(obj.read(), [])

    def test_primary_cannot_be_bought_twice(self):
        with tempfile.TemporaryDirectory() as d:
            obj = ledger(Path(d) / "purchases-v1.json")
            self.assertTrue(obj.reserve(fqn=FIRST, datacenter="bhs", total=AMOUNT))
            self.assertFalse(obj.reserve(fqn=FIRST, datacenter="bhs", total=AMOUNT))

    def test_unknown_outcome_permanently_blocks_next_purchase(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "purchases-v1.json"
            obj = ledger(path)
            obj.reserve(fqn=FIRST, datacenter="bhs", total=AMOUNT)
            obj.update_outcome(plan_code="27skle06-v1", status="unknown")
            with self.assertRaises(guard.PurchaseSafetyError):
                ledger(path).reserve(fqn=SECOND, datacenter="gra", total=AMOUNT)
            with self.assertRaises(guard.PurchaseSafetyError):
                obj.update_outcome(plan_code="27skle06-v1", status="confirmed", order_id=12345)

    def test_legacy_journal_requires_migration(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "purchases-v1.json"
            (Path(d) / "intent.json").write_text("unknown legacy status")
            with self.assertRaisesRegex(guard.PurchaseSafetyError, "migration"):
                ledger(path).reserve(fqn=FIRST, datacenter="bhs", total=AMOUNT)

    def test_corrupted_or_public_ledger_blocks_purchase(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "purchases-v1.json"
            path.write_text("{invalid json")
            path.chmod(0o600)
            with self.assertRaises(guard.PurchaseSafetyError):
                ledger(path).reserve(fqn=FIRST, datacenter="bhs", total=AMOUNT)
            path.write_text(json.dumps({"schema": 1, "attempts": []}))
            path.chmod(0o644)
            with self.assertRaisesRegex(guard.PurchaseSafetyError, "0600"):
                ledger(path).reserve(fqn=FIRST, datacenter="bhs", total=AMOUNT)

    def test_max_server_policy_never_accepts_three(self):
        for value in ("0", "3", "4", "many", "2.0", "-1"):
            with self.subTest(value=value):
                with self.assertRaises(guard.PurchaseSafetyError):
                    guard.PurchasePolicy.from_env({
                        "OVH_AUTO_BUY_MAX_TOTAL_EUR": "45.00",
                        "OVH_AUTO_BUY_MAX_SERVERS": value
                    })
        self.assertEqual(
            guard.PurchasePolicy.from_env({
                "OVH_AUTO_BUY_MAX_TOTAL_EUR": "45.00"
            }).max_servers, 2
        )

    def test_requires_exact_fqn_and_two_distinct_models(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "purchases-v1.json"
            with self.assertRaises(guard.PurchaseSafetyError):
                ledger(path, approved=(FIRST, FIRST))
            with self.assertRaises(guard.PurchaseSafetyError):
                ledger(path, approved=(SECOND,))
            with self.assertRaises(guard.PurchaseSafetyError):
                ledger(path).reserve(fqn=THIRD, datacenter="bhs", total=AMOUNT)

    def test_read_only_cli_reports_zero_of_two_without_creating_files(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "purchases-v1.json"
            with patch.dict(os.environ, {
                "OVH_AUTO_BUY_QUOTA_JOURNAL": str(path),
                "OVH_AUTO_BUY_MAX_SERVERS": "2",
            }):
                with patch("builtins.print") as output:
                    self.assertEqual(q.run_quota_status(), 0)
            self.assertFalse(path.exists())
            self.assertIn("0/2", output.call_args_list[0].args[0])
            self.assertIn("NOT YET APPROVED", output.call_args_list[2].args[0])

    def test_read_only_cli_warns_about_legacy_journal(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "purchases-v1.json"
            (Path(d) / "intent.json").write_text("previous attempt")
            with patch.dict(os.environ, {"OVH_AUTO_BUY_QUOTA_JOURNAL": str(path)}):
                with patch("builtins.print") as output:
                    self.assertEqual(q.run_quota_status(), 1)
            self.assertIn("Legacy intent", output.call_args.args[0])
            self.assertFalse(path.exists())

    def test_wrong_outcome_sequence_or_order_id_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            obj = ledger(Path(d) / "purchases-v1.json")
            obj.reserve(fqn=FIRST, datacenter="bhs", total=AMOUNT)
            with self.assertRaises(guard.PurchaseSafetyError):
                obj.update_outcome(plan_code="27skle06-v1", status="confirmed", order_id=1)
            obj.update_outcome(plan_code="27skle06-v1", status="submitted", order_id=100)
            with self.assertRaises(guard.PurchaseSafetyError):
                obj.update_outcome(plan_code="27skle06-v1", status="confirmed", order_id=101)
            self.assertEqual(obj.read()[0]["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
