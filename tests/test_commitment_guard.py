"""Only monthly/no-fixed-term candidates are allowed; no network calls."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commitment_guard import CommitmentSafetyError, inspect_commitment
from order_probe import OrderProbeError, find_monthly_offer, PLAN_CODE


class CommitmentGuardTests(unittest.TestCase):
    def test_missing_contract_fields_are_unverified_not_zero_commitment(self):
        self.assertFalse(inspect_commitment({"duration": "P1M", "contracts": ["legal terms"]}))

    def test_explicit_zero_commitment_metadata_is_detected(self):
        for info in ({"commitmentMonths": 0}, {"commitment": False},
                     {"engagementDuration": "P0M"}, {"commitment": "sans_engagement"}):
            with self.subTest(info=info):
                self.assertTrue(inspect_commitment(info))

    def test_any_positive_months_blocks_even_when_p1m_is_selected(self):
        for metadata in ({"commitmentMonths": 12}, {"engagement": True},
                         {"engagementDuration": "P24M"}, {"commitment": "12m"}):
            with self.subTest(metadata=metadata):
                with self.assertRaisesRegex(CommitmentSafetyError, "Fixed-term"):
                    inspect_commitment({"duration": "P1M", **metadata})

    def test_nested_checkout_contract_rejects_fixed_term(self):
        with self.assertRaisesRegex(CommitmentSafetyError, "Fixed-term"):
            inspect_commitment({"contracts": [{"engagementDuration": "P12M"}]})

    def test_unknown_commitment_data_not_silently_assumed_safe(self):
        for info in ({"engagementDuration": "forever"}, {"commitment": {"special": 1}},
                     {"commitmentMonths": True}):
            with self.subTest(info=info):
                with self.assertRaises(CommitmentSafetyError):
                    inspect_commitment(info)

    def test_p1m_offer_with_12_month_commitment_is_rejected(self):
        offers = [{"planCode": PLAN_CODE, "prices": [{
            "duration": "P1M", "pricingMode": "default", "commitmentMonths": 12,
        }]}]
        with self.assertRaisesRegex(OrderProbeError, "Fixed-term"):
            find_monthly_offer(offers)

    def test_clean_p1m_default_still_selected_without_guarantee(self):
        offers = [{"planCode": PLAN_CODE, "prices": [
            {"duration": "P12M", "pricingMode": "default"},
            {"duration": "P1M", "pricingMode": "default"},
            {"duration": "P24M", "pricingMode": "default"},
        ]}]
        self.assertEqual(find_monthly_offer(offers), {
            "planCode": PLAN_CODE, "duration": "P1M", "pricingMode": "default",
        })


if __name__ == "__main__":
    unittest.main()
