"""Offline, no-network tests of the future auto-buy decision gate."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_buy_guard import PurchaseEvidence, assess_auto_buy
from ksle_monitor import Stock
from purchase_guard import APPROVED_FQNS, STORAGE_PRIORITIES, PurchasePolicy


def policy() -> PurchasePolicy:
    return PurchasePolicy.from_env({
        "OVH_AUTO_BUY_ENABLED": "false",
        "OVH_AUTO_BUY_MAX_TOTAL_EUR": "80.00",
        "OVH_AUTO_BUY_MAX_MONTHLY_EUR": "37.00",
    })


def stock(index: int = 0, dc: str = "bhs", status: str = "1H-high") -> Stock:
    return Stock(APPROVED_FQNS[index], STORAGE_PRIORITIES[index], dc, status)


def evidence(used=0) -> PurchaseEvidence:
    # Synthetic evidence solely for the offline test environment. The live
    # commercial audit never asserts these legal/renewal proof flags.
    return PurchaseEvidence(
        initial_including_vat=Decimal("80.00"), initial_quote_verified=True,
        monthly_including_vat=Decimal("37.00"), monthly_renewal_verified=True,
        no_fixed_commitment_verified=True, payment_method_ready=True,
        remote_slots_used=used, remote_inventory_verified=True,
        local_purchase_history_clear=True,
    )


class AutoBuyGateTests(unittest.TestCase):
    def test_synthetic_proof_can_pass_only_in_dry_run_no_checkout(self):
        result = assess_auto_buy(policy(), stock(), evidence())
        self.assertTrue(result.simulated_criteria_met)
        self.assertEqual(result.remaining_slots, 2)
        self.assertIn("DRY RUN", result.summary())
        self.assertIn("NO REAL CHECKOUT IMPLEMENTED OR AUTHORIZED", result.summary())

    def test_all_four_approved_disks_and_regions_can_pass(self):
        for index in range(4):
            for dc in policy().datacenters:
                with self.subTest(index=index, dc=dc):
                    result = assess_auto_buy(policy(), stock(index, dc), evidence())
                    self.assertTrue(result.simulated_criteria_met)

    def test_two_sequential_orders_maximum_even_if_same_disks(self):
        # Simulated progression after each remote account reconciliation.
        first = assess_auto_buy(policy(), stock(), evidence(0))
        second = assess_auto_buy(policy(), stock(), evidence(1))
        third = assess_auto_buy(policy(), stock(), evidence(2))
        self.assertTrue(first.simulated_criteria_met)
        self.assertTrue(second.simulated_criteria_met)
        self.assertEqual((first.remaining_slots, second.remaining_slots), (2, 1))
        self.assertFalse(third.simulated_criteria_met)
        self.assertIn("quota reached", third.summary())

    def test_two_different_disks_use_same_global_quota(self):
        first = assess_auto_buy(policy(), stock(0), evidence(0))
        second = assess_auto_buy(policy(), stock(3, "waw"), evidence(1))
        self.assertTrue(first.simulated_criteria_met)
        self.assertTrue(second.simulated_criteria_met)

    def test_missing_monthly_invoice_proof_is_blocked(self):
        for monthly, verified in (
            (None, False), (Decimal("32.00"), False),
            (Decimal("0.00"), True), (Decimal("39.00"), True),
        ):
            with self.subTest(monthly=monthly, verified=verified):
                actual = replace(evidence(), monthly_including_vat=monthly,
                                 monthly_renewal_verified=verified)
                result = assess_auto_buy(policy(), stock(), actual)
                self.assertFalse(result.simulated_criteria_met)
                self.assertIn("monthly", result.summary())

    def test_unverified_or_fixed_commitment_never_passes(self):
        actual = replace(evidence(), no_fixed_commitment_verified=False)
        self.assertIn("12/24-month commitment not verified",
                      assess_auto_buy(policy(), stock(), actual).summary())

    def test_initial_price_over_cap_or_missing_tax_proof_blocks(self):
        for initial, valid in (
            (None, False), (Decimal("79.00"), False),
            (Decimal("80.01"), True), (Decimal("0"), True),
            (Decimal("73.009"), True), (79.00, True),
        ):
            with self.subTest(initial=initial, valid=valid):
                actual = replace(evidence(), initial_including_vat=initial,
                                 initial_quote_verified=valid)
                self.assertFalse(assess_auto_buy(policy(), stock(), actual).simulated_criteria_met)

    def test_invalid_stock_and_datacenter_never_pass(self):
        wrong_model = Stock("27skle01-v1.ram-128g-ddr4-ecc-2400.softraid-2x1920nvme",
                            STORAGE_PRIORITIES[1], "bhs", "1H-high")
        wrong_country = stock(0, "sgp")
        unavailable = stock(0, "bhs", "unavailable")
        for option in (None, wrong_model, wrong_country, unavailable):
            with self.subTest(option=option):
                self.assertFalse(assess_auto_buy(policy(), option, evidence()).simulated_criteria_met)

    def test_quota_unknown_out_of_range_or_unreconciled_blocks(self):
        for used, verified, clear in (
            (None, False, True), (0, False, True),
            (3, True, True), (0, True, False),
        ):
            with self.subTest(used=used, verified=verified, clear=clear):
                actual = replace(evidence(), remote_slots_used=used,
                                 remote_inventory_verified=verified,
                                 local_purchase_history_clear=clear)
                self.assertFalse(assess_auto_buy(policy(), stock(), actual).simulated_criteria_met)

    def test_payment_method_must_be_verified(self):
        actual = replace(evidence(), payment_method_ready=False)
        self.assertIn("payment method not verified",
                      assess_auto_buy(policy(), stock(), actual).summary())

    def test_attempt_to_enable_live_policy_is_rejected(self):
        enabled = replace(policy(), enabled=True)
        result = assess_auto_buy(enabled, stock(), evidence())
        self.assertFalse(result.simulated_criteria_met)
        self.assertIn("switch is not authorized", result.summary())


if __name__ == "__main__":
    unittest.main()
