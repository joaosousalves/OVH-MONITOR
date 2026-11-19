"""Global two-slot KS-LE-6 purchasing safety tests. No network calls."""
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import purchase_quota as quota
import purchase_guard as guard

HYBRID = guard.APPROVED_FQNS[0]
NVME = guard.APPROVED_FQNS[1]
THREE_NVME = guard.APPROVED_FQNS[2]
FOUR_SSD = guard.APPROVED_FQNS[3]
PRICE = Decimal("39.99")


def ledger(directory: str, max_servers: int = 2):
    return quota.PurchaseQuota(Path(directory) / "purchases-v2.json",
                               max_servers=max_servers)


def confirm_first(q: quota.PurchaseQuota, order_id: int = 12345):
    q.update_outcome(slot=1, status="submitted", order_id=order_id)
    q.update_outcome(slot=1, status="confirmed", order_id=order_id)


class PurchaseQuotaTests(unittest.TestCase):
    def test_two_identical_ks_le_6_fqns_are_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            q = ledger(d)
            self.assertEqual(q.reserve(fqn=HYBRID, datacenter="bhs", total=PRICE), 1)
            confirm_first(q)
            restarted = ledger(d)
            self.assertEqual(restarted.reserve(fqn=HYBRID, datacenter="bhs", total=PRICE), 2)
            restarted.update_outcome(slot=2, status="submitted", order_id=23456)
            restarted.update_outcome(slot=2, status="confirmed", order_id=23456)
            self.assertIsNone(ledger(d).reserve(fqn=HYBRID, datacenter="bhs", total=PRICE))
            self.assertEqual([x["slot"] for x in ledger(d).read()], [1, 2])
            self.assertEqual([x["planCode"] for x in ledger(d).read()],
                             [guard.PLAN_CODE, guard.PLAN_CODE])

    def test_two_different_disk_fqns_use_same_global_cap(self):
        with tempfile.TemporaryDirectory() as d:
            q = ledger(d)
            self.assertEqual(q.reserve(fqn=HYBRID, datacenter="bhs", total=PRICE), 1)
            confirm_first(q)
            self.assertEqual(q.reserve(fqn=THREE_NVME, datacenter="gra", total=PRICE), 2)
            self.assertIsNone(q.reserve(fqn=NVME, datacenter="waw", total=PRICE))
            self.assertEqual([x["fqn"] for x in q.read()], [HYBRID, THREE_NVME])

    def test_all_four_128gb_disk_configs_are_eligible(self):
        for fqn in guard.APPROVED_FQNS:
            with self.subTest(fqn=fqn), tempfile.TemporaryDirectory() as d:
                self.assertEqual(ledger(d).reserve(
                    fqn=fqn, datacenter="lon", total=PRICE), 1)

    def test_other_plan_or_ram_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            q = ledger(d)
            for fqn in (
                HYBRID.replace("27skle06-v1", "27skle05-v1"),
                HYBRID.replace("ram-128g", "ram-64g"),
                HYBRID + ".unexpected",
                "27skle01-v1.ram-16g.softraid-3x2000sa",
            ):
                with self.subTest(fqn=fqn):
                    with self.assertRaises(guard.PurchaseSafetyError):
                        q.reserve(fqn=fqn, datacenter="bhs", total=PRICE)
            self.assertEqual(q.read(), [])

    def test_unapproved_datacenter_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            q = ledger(d)
            for dc in ("hil", "sgp", "syd", "vin", "rbx3", "usa", "ca", ""):
                with self.subTest(dc=dc):
                    with self.assertRaises(guard.PurchaseSafetyError):
                        q.reserve(fqn=NVME, datacenter=dc, total=PRICE)

    def test_region_allowlist_contains_only_europe_and_canada(self):
        self.assertEqual(set(guard.DATACENTERS),
                         {"bhs", "fra", "gra", "lon", "rbx", "sbg", "waw"})
        self.assertEqual(set(guard.DATACENTERS), set(quota.DATACENTERS))

    def test_unresolved_first_order_blocks_second_even_with_other_disk(self):
        with tempfile.TemporaryDirectory() as d:
            q = ledger(d)
            self.assertEqual(q.reserve(fqn=HYBRID, datacenter="bhs", total=PRICE), 1)
            with self.assertRaisesRegex(guard.PurchaseSafetyError, "unresolved"):
                ledger(d).reserve(fqn=NVME, datacenter="gra", total=PRICE)
            q.update_outcome(slot=1, status="unknown")
            with self.assertRaisesRegex(guard.PurchaseSafetyError, "unresolved"):
                q.reserve(fqn=HYBRID, datacenter="bhs", total=PRICE)
            self.assertEqual(len(q.read()), 1)

    def test_submitted_first_order_blocks_until_confirmed(self):
        with tempfile.TemporaryDirectory() as d:
            q = ledger(d)
            q.reserve(fqn=NVME, datacenter="waw", total=PRICE)
            q.update_outcome(slot=1, status="submitted", order_id=1001)
            with self.assertRaises(guard.PurchaseSafetyError):
                q.reserve(fqn=NVME, datacenter="waw", total=PRICE)
            q.update_outcome(slot=1, status="confirmed", order_id=1001)
            self.assertEqual(q.reserve(fqn=NVME, datacenter="waw", total=PRICE), 2)

    def test_one_server_policy_blocks_second(self):
        with tempfile.TemporaryDirectory() as d:
            q = ledger(d, max_servers=1)
            q.reserve(fqn=FOUR_SSD, datacenter="rbx", total=PRICE)
            confirm_first(q)
            self.assertIsNone(q.reserve(fqn=HYBRID, datacenter="bhs", total=PRICE))

    def test_legacy_ledger_stops_reservations(self):
        for legacy_name in ("intent.json", "purchases-v1.json"):
            with self.subTest(legacy_name=legacy_name), tempfile.TemporaryDirectory() as d:
                Path(d, legacy_name).write_text("unknown historic records")
                with self.assertRaisesRegex(guard.PurchaseSafetyError, "reconciliation"):
                    ledger(d).reserve(fqn=HYBRID, datacenter="bhs", total=PRICE)
                self.assertFalse(Path(d, "purchases-v2.json").exists())

    def test_bad_schema_corrupt_json_public_file_never_reset(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "purchases-v2.json"
            for content in ("not json", json.dumps({"schema": 1, "attempts": []})):
                path.write_text(content, encoding="utf-8")
                path.chmod(0o600)
                with self.assertRaises(guard.PurchaseSafetyError):
                    ledger(d).reserve(fqn=HYBRID, datacenter="bhs", total=PRICE)
            path.write_text(json.dumps({"schema": 2, "attempts": []}))
            path.chmod(0o644)
            with self.assertRaisesRegex(guard.PurchaseSafetyError, "0600"):
                ledger(d).reserve(fqn=HYBRID, datacenter="bhs", total=PRICE)

    def test_disallowed_purchase_outcome_transition(self):
        with tempfile.TemporaryDirectory() as d:
            q = ledger(d)
            q.reserve(fqn=HYBRID, datacenter="bhs", total=PRICE)
            with self.assertRaises(guard.PurchaseSafetyError):
                q.update_outcome(slot=1, status="confirmed", order_id=123)
            with self.assertRaises(guard.PurchaseSafetyError):
                q.update_outcome(slot=2, status="submitted", order_id=123)
            q.update_outcome(slot=1, status="submitted", order_id=123)
            with self.assertRaises(guard.PurchaseSafetyError):
                q.update_outcome(slot=1, status="confirmed", order_id=124)
            q.update_outcome(slot=1, status="confirmed", order_id=123)
            with self.assertRaises(guard.PurchaseSafetyError):
                q.update_outcome(slot=1, status="unknown")

    def test_ledger_rejects_duplicate_slots_and_unexpected_fqn(self):
        with tempfile.TemporaryDirectory() as d:
            q = ledger(d)
            q.reserve(fqn=HYBRID, datacenter="bhs", total=PRICE)
            record = q.read()[0]
            payload = {"schema": 2, "attempts": [record, record]}
            q.path.write_text(json.dumps(payload), encoding="utf-8")
            q.path.chmod(0o600)
            with self.assertRaises(guard.PurchaseSafetyError):
                q.read()
            record["fqn"] = HYBRID.replace("128g", "64g")
            q.path.write_text(json.dumps({"schema": 2, "attempts": [record]}))
            with self.assertRaises(guard.PurchaseSafetyError):
                q.read()

    def test_read_only_cli_reports_zero_of_two(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "purchases-v2.json"
            with patch.dict(os.environ, {
                "OVH_AUTO_BUY_QUOTA_JOURNAL": str(path),
                "OVH_AUTO_BUY_MAX_SERVERS": "2",
            }):
                with patch("builtins.print") as output:
                    self.assertEqual(quota.run_quota_status(), 0)
            self.assertFalse(path.exists())
            self.assertIn("0/2", output.call_args_list[0].args[0])
            self.assertIn("Same or different disks", output.call_args_list[-2].args[0])

    def test_read_only_cli_detects_old_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "purchases-v2.json"
            Path(d, "purchases-v1.json").write_text("previous")
            with patch.dict(os.environ, {"OVH_AUTO_BUY_QUOTA_JOURNAL": str(path)}):
                with patch("builtins.print") as output:
                    self.assertEqual(quota.run_quota_status(), 1)
            self.assertIn("requires reconciliation", output.call_args.args[0])
            self.assertFalse(path.exists())

    def test_max_two_policy_never_three(self):
        for value in ("0", "3", "4", "2.0", "many"):
            with self.subTest(value=value):
                with self.assertRaises(guard.PurchaseSafetyError):
                    guard.PurchasePolicy.from_env({
                        "OVH_AUTO_BUY_MAX_TOTAL_EUR": "45.00",
                        "OVH_AUTO_BUY_MAX_SERVERS": value,
                    })


if __name__ == "__main__":
    unittest.main()
