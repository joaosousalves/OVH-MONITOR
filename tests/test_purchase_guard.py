"""No-network tests for disabled KS-LE-6 automated purchase preparation."""
from __future__ import annotations

from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ksle_monitor as monitor
import purchase_guard as guard


ENV = {
    "OVH_AUTO_BUY_ENABLED": "false",
    "OVH_AUTO_BUY_MAX_TOTAL_EUR": "40.00",
    "OVH_AUTO_BUY_MAX_MONTHLY_EUR": "37.00",
    "OVH_AUTO_BUY_ALLOWED_DATACENTERS": "bhs,gra,rbx",
}


def policy():
    return guard.PurchasePolicy.from_env(ENV)


def stock(dc="bhs", status="1H-high", fqn=guard.EXPECTED_FQN, storage=monitor.PREFERRED_STORAGE):
    return monitor.Stock(fqn, storage, dc, status)


def checkout(amount="33.20", currency="EUR"):
    return {"prices": {"withTax": {"value": amount, "currencyCode": currency, "text": "33,20 EUR"}}}


class PurchaseGuardTests(unittest.TestCase):
    def test_purchase_disabled_by_default_even_if_price_configured(self):
        p = guard.PurchasePolicy.from_env({
            "OVH_AUTO_BUY_MAX_TOTAL_EUR": "40.00",
            "OVH_AUTO_BUY_MAX_MONTHLY_EUR": "37.00",
        })
        self.assertFalse(p.enabled)

    def test_two_independent_user_approved_price_caps(self):
        p = guard.PurchasePolicy.from_env({
            "OVH_AUTO_BUY_MAX_TOTAL_EUR": "80.00",
            "OVH_AUTO_BUY_MAX_MONTHLY_EUR": "37.00",
        })
        self.assertEqual(p.max_total_eur, Decimal("80.00"))
        self.assertEqual(p.max_monthly_eur, Decimal("37.00"))

    def test_monthly_limit_required_and_must_be_positive_eur(self):
        with self.assertRaisesRegex(guard.PurchaseSafetyError, "OVH_AUTO_BUY_MAX_MONTHLY_EUR"):
            guard.PurchasePolicy.from_env({"OVH_AUTO_BUY_MAX_TOTAL_EUR": "80.00"})
        for amount in ("0", "-1", "abc", "37.001", "501.00", "NaN", "1e2"):
            with self.subTest(amount=amount):
                with self.assertRaises(guard.PurchaseSafetyError):
                    guard.PurchasePolicy.from_env({
                        "OVH_AUTO_BUY_MAX_TOTAL_EUR": "80.00",
                        "OVH_AUTO_BUY_MAX_MONTHLY_EUR": amount,
                    })

    def test_attempted_enable_fails_closed(self):
        with self.assertRaisesRegex(guard.PurchaseSafetyError, "NOT implemented"):
            guard.PurchasePolicy.from_env({
                "OVH_AUTO_BUY_ENABLED": "true", "OVH_AUTO_BUY_MAX_TOTAL_EUR": "40.00"
            })

    def test_missing_price_limit_fails_closed(self):
        with self.assertRaises(guard.PurchaseSafetyError):
            guard.PurchasePolicy.from_env({"OVH_AUTO_BUY_ENABLED": "false"})

    def test_wrong_or_excessive_price_limit_fails_closed(self):
        for amount in ("0", "-1", "abc", "40.999", "501.00", "NaN", "1e3"):
            with self.subTest(amount=amount):
                with self.assertRaises(guard.PurchaseSafetyError):
                    guard.PurchasePolicy.from_env({
                        "OVH_AUTO_BUY_MAX_TOTAL_EUR": amount,
                        "OVH_AUTO_BUY_MAX_MONTHLY_EUR": "37.00",
                    })

    def test_duplicate_or_unknown_datacenter_rejected(self):
        for choices in ("bhs,bhs", "bhs,xxx", "", ",gra"):
            with self.subTest(choices=choices):
                with self.assertRaises(guard.PurchaseSafetyError):
                    guard.PurchasePolicy.from_env({
                        "OVH_AUTO_BUY_MAX_TOTAL_EUR": "40",
                        "OVH_AUTO_BUY_MAX_MONTHLY_EUR": "37.00",
                        "OVH_AUTO_BUY_ALLOWED_DATACENTERS": choices,
                    })

    def test_structured_eur_quote_total(self):
        self.assertEqual(guard.checkout_total_eur(checkout(39.34)), Decimal("39.34"))
        self.assertEqual(guard.checkout_total_eur(checkout("33.20")), Decimal("33.20"))

    def test_quote_fail_closed_on_missing_currency_or_value(self):
        samples = [
            {}, {"prices": {}},
            {"prices": {"withTax": {"text": "33.20 EUR", "currencyCode": "EUR"}}},
            checkout("10.00", "USD"),
            checkout("-1.00"),
            checkout("100.123"),
            checkout(0),
        ]
        for value in samples:
            with self.subTest(value=value):
                with self.assertRaises(guard.PurchaseSafetyError):
                    guard.checkout_total_eur(value)

    def test_choose_only_exact_hardware_in_allowed_location(self):
        other_ram = guard.EXPECTED_FQN.replace("ram-128g", "ram-64g")
        items = [stock("bhs", "1H-high", fqn=other_ram), stock("rbx"), stock("gra")]
        self.assertEqual(guard.choose_candidate(items, policy()).datacenter, "gra")
        self.assertIsNone(guard.choose_candidate([
            stock("bhs", "unavailable"), stock("gra", "unknown")
        ], policy()))

    def test_four_ks_le_6_disks_share_128gb_ram_and_rank_by_storage(self):
        self.assertEqual(len(guard.APPROVED_FQNS), 4)
        self.assertEqual(guard.APPROVED_FQNS[0], guard.EXPECTED_FQN)
        self.assertTrue(all(fqn.startswith(
            "27skle06-v1.ram-128g-ddr4-ecc-2400."
        ) for fqn in guard.APPROVED_FQNS))
        # The highest-ranked disk wins, even at a later-priority datacenter.
        items = [
            stock("bhs", fqn=guard.APPROVED_FQNS[1], storage=guard.STORAGE_PRIORITIES[1]),
            stock("waw", fqn=guard.APPROVED_FQNS[0], storage=guard.STORAGE_PRIORITIES[0]),
        ]
        p = guard.PurchasePolicy.from_env({
            **ENV, "OVH_AUTO_BUY_ALLOWED_DATACENTERS": "bhs,waw",
        })
        self.assertEqual(guard.choose_candidate(items, p).fqn, guard.EXPECTED_FQN)

    def test_fallback_to_next_disk_after_preferred_unavailable(self):
        items = [
            stock("bhs", "unavailable"),
            stock("gra", "72H", fqn=guard.APPROVED_FQNS[1],
                  storage=guard.STORAGE_PRIORITIES[1]),
            stock("bhs", "1H-high", fqn=guard.APPROVED_FQNS[2],
                  storage=guard.STORAGE_PRIORITIES[2]),
        ]
        self.assertEqual(guard.choose_candidate(items, policy()).fqn,
                         guard.APPROVED_FQNS[1])

    def test_candidate_rejects_non_matching_ram_disk_and_region(self):
        items = [
            stock("bhs", fqn=guard.APPROVED_FQNS[2],
                  storage=guard.STORAGE_PRIORITIES[1]),
            stock("bhs", fqn=guard.APPROVED_FQNS[1].replace("128g", "64g"),
                  storage=guard.STORAGE_PRIORITIES[1]),
            stock("syd", fqn=guard.APPROVED_FQNS[1],
                  storage=guard.STORAGE_PRIORITIES[1]),
        ]
        self.assertIsNone(guard.choose_candidate(items, policy()))

    def test_storage_preference_status_report_shows_all_four_and_eu_ca_only(self):
        items = [
            stock("bhs", "unavailable"),
            stock("fra", "1H-high", fqn=guard.APPROVED_FQNS[1],
                  storage=guard.STORAGE_PRIORITIES[1]),
            stock("syd", "1H-high", fqn=guard.APPROVED_FQNS[2],
                  storage=guard.STORAGE_PRIORITIES[2]),
        ]
        report = guard.render_disk_preferences(items)
        self.assertIn("1. 2 x 512 GB NVMe + 2 x 4 TB HDD", report)
        self.assertIn("2. 2 x 1.92 TB NVMe", report)
        self.assertIn("3. 3 x 1 TB NVMe", report)
        self.assertIn("4. 4 x 960 GB SATA SSD", report)
        self.assertIn("FRA  1H-high", report)
        self.assertIn("Germany (FRA)", report)
        self.assertEqual(guard.DATACENTERS, monitor.ALLOWED_DATACENTERS)
        self.assertNotIn("SYD", report)

    def test_purchase_preview_checks_global_slots_before_cart(self):
        from purchase_quota import PurchaseQuota
        with tempfile.TemporaryDirectory() as d:
            q = PurchaseQuota(Path(d) / "purchases-v2.json",
                              approved_fqns=guard.APPROVED_FQNS)
            legacy = guard.OrderJournal(Path(d) / "intent.json")
            self.assertEqual(q.reserve(
                fqn=guard.EXPECTED_FQN, datacenter="bhs",
                total=Decimal("33.20")), 1)
            with patch.object(guard, "verify_available_test_fqn") as verify:
                with patch.object(guard, "test_order") as cart:
                    with self.assertRaisesRegex(guard.PurchaseSafetyError, "reconciled"):
                        guard.preview(policy(), stocks=[stock()], client=object(),
                                      journal=legacy, quota=q)
            verify.assert_not_called()
            cart.assert_not_called()
            q.update_outcome(slot=1, status="submitted", order_id=100)
            q.update_outcome(slot=1, status="confirmed", order_id=100)
            self.assertEqual(q.reserve(
                fqn=guard.APPROVED_FQNS[1], datacenter="gra",
                total=Decimal("35.00")), 2)
            with patch.object(guard, "test_order") as cart:
                outcome = guard.preview(policy(), stocks=[stock()], client=object(),
                                        journal=legacy, quota=q)
            self.assertIn("LIMIT REACHED", outcome)
            cart.assert_not_called()

    def test_manual_disk_list_cli_no_cart(self):
        with patch.object(guard, "retrieve_stock", return_value=[stock()]):
            with patch("builtins.print") as print_result:
                self.assertEqual(monitor.main(["--list-ksle6-disks"]), 0)
        self.assertIn("KS-LE-6 / 128 GB RAM", print_result.call_args.args[0])

    def test_no_stock_does_not_create_cart_or_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = guard.OrderJournal(Path(directory) / "intent.json")
            with patch.object(guard, "verify_available_test_fqn") as preflight:
                with patch.object(guard, "test_order") as cart:
                    result = guard.preview(policy(), stocks=[stock("bhs", "unavailable")],
                                           client=None, journal=ledger)
            self.assertIn("WAITING", result)
            self.assertIsNone(ledger.read())
            preflight.assert_not_called()
            cart.assert_not_called()

    def test_empty_stock_feed_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(guard.PurchaseSafetyError, "no rows"):
                guard.preview(policy(), stocks=[], client=None,
                              journal=guard.OrderJournal(Path(directory) / "intent.json"))

    def test_eligible_quote_under_cap_never_reserves(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = guard.OrderJournal(Path(directory) / "intent.json")
            def fake_quote(*args, **kwargs):
                self.assertEqual(kwargs["plan_code"], monitor.PLAN_CODE)
                self.assertEqual(kwargs["target_fqn"], guard.EXPECTED_FQN)
                kwargs["quote_validator"](checkout("33.20"))
                return "cart quoted"
            with patch.object(guard, "verify_available_test_fqn") as preflight:
                with patch.object(guard, "test_order", side_effect=fake_quote) as cart:
                    result = guard.preview(policy(), stocks=[stock()], client=object(),
                                           journal=journal)
            self.assertIn("PREVIEW ONLY", result)
            self.assertIn("33.20 EUR", result)
            self.assertIn("No order/payment submitted", result)
            self.assertIsNone(journal.read())
            preflight.assert_called_once_with(monitor.PLAN_CODE, guard.EXPECTED_FQN, "bhs")
            cart.assert_called_once()

    def test_price_cap_enforced_on_validated_quote(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = guard.OrderJournal(Path(directory) / "intent.json")
            def expensive_quote(*args, **kwargs):
                kwargs["quote_validator"](checkout("45.00"))
            with patch.object(guard, "verify_available_test_fqn"):
                with patch.object(guard, "test_order", side_effect=expensive_quote):
                    with self.assertRaisesRegex(guard.PurchaseSafetyError, "PRICE BLOCKED"):
                        guard.preview(policy(), stocks=[stock()], client=object(), journal=journal)
            self.assertIsNone(journal.read())

    def test_journal_reservation_persistent_and_one_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intent.json"
            first = guard.OrderJournal(path)
            self.assertTrue(first.reserve_once(
                fqn=guard.EXPECTED_FQN, datacenter="bhs", total=Decimal("33.20")))
            second = guard.OrderJournal(path)
            self.assertFalse(second.reserve_once(
                fqn=guard.EXPECTED_FQN, datacenter="gra", total=Decimal("33.20")))
            record = second.read()
            self.assertEqual(record["status"], "reserved")
            self.assertEqual(record["datacenter"], "bhs")
            self.assertEqual(record["totalEur"], "33.20")
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_existing_reserved_journal_blocks_cart(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = guard.OrderJournal(Path(directory) / "intent.json")
            journal.reserve_once(
                fqn=guard.EXPECTED_FQN, datacenter="bhs", total=Decimal("33.20"))
            with patch.object(guard, "verify_available_test_fqn") as preflight:
                with self.assertRaisesRegex(guard.PurchaseSafetyError, "Reconcile"):
                    guard.preview(policy(), stocks=[stock()], client=object(), journal=journal)
            preflight.assert_not_called()

    def test_corrupted_journal_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intent.json"
            path.write_text("NOT JSON", encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaises(guard.PurchaseSafetyError):
                guard.OrderJournal(path).read()

    def test_public_journal_permissions_block(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intent.json"
            path.write_text(json.dumps({"schema": 1, "status": "reserved",
                                        "planCode": monitor.PLAN_CODE}), encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaisesRegex(guard.PurchaseSafetyError, "0600"):
                guard.OrderJournal(path).read()

    def test_journal_refuses_non_target_product(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = guard.OrderJournal(Path(directory) / "intent.json")
            with self.assertRaises(guard.PurchaseSafetyError):
                journal.reserve_once(
                    fqn="27skle01-v1.ram-32g.softraid-3x2000sa",
                    datacenter="bhs", total=Decimal("33.20"))
            self.assertIsNone(journal.read())

    def test_no_live_cart_when_activated_accidentally(self):
        with patch.object(guard, "retrieve_stock") as stock_call:
            with patch.dict(os.environ, {
                "OVH_AUTO_BUY_ENABLED": "true", "OVH_AUTO_BUY_MAX_TOTAL_EUR": "40.00"
            }):
                self.assertEqual(guard.run_purchase_preview(), 1)
            stock_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()

