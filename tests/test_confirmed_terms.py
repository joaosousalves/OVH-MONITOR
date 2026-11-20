"""Owner-confirmed price baseline is offline; never authorizes checkout."""
from decimal import Decimal
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import confirmed_terms as terms
import ksle_monitor as monitor


class ConfirmedTermsTests(unittest.TestCase):
    def test_exact_four_layouts_in_purchase_priority(self):
        expected = (
            ("33.20", "66.40"),
            ("34.43", "67.63"),
            ("35.66", "68.86"),
            ("36.89", "70.09"),
        )
        self.assertEqual(tuple(terms.NET_MONTHLY_BY_STORAGE), monitor.STORAGE_PRIORITIES)
        for storage, (monthly, initial) in zip(monitor.STORAGE_PRIORITIES, expected):
            with self.subTest(storage=storage):
                ref = terms.for_storage(storage)
                self.assertEqual(ref.monthly_gross, Decimal(monthly))
                self.assertEqual(ref.first_payment_gross, Decimal(initial))
                self.assertLessEqual(ref.monthly_gross, terms.MONTHLY_CAP)
                self.assertLessEqual(ref.first_payment_gross, terms.INITIAL_CAP)

    def test_unknown_storage_never_has_reference(self):
        with self.assertRaises(ValueError):
            terms.for_storage("softraid-other")

    def test_compare_cart_detects_price_drift_even_within_caps(self):
        ref = terms.for_storage(monitor.STORAGE_PRIORITIES[0])
        result = terms.compare_cart(
            ref, initial_gross=Decimal("70.00"), monthly_gross=Decimal("35.00")
        )
        self.assertIn("INITIAL=ABOVE_CONFIRMED_REFERENCE", result)
        self.assertIn("MONTHLY=ABOVE_CONFIRMED_REFERENCE", result)
        self.assertIn("No fixed-commitment or final renewal proof is inferred", result)

    def test_unknown_monthly_stays_unverified(self):
        ref = terms.for_storage(monitor.STORAGE_PRIORITIES[3])
        result = terms.compare_cart(
            ref, initial_gross=Decimal("70.09"), monthly_gross=None
        )
        self.assertIn("INITIAL=NOT_ABOVE_REFERENCE", result)
        self.assertIn("MONTHLY=UNVERIFIED", result)

    def test_invalid_quote_amounts_rejected(self):
        ref = terms.for_storage(monitor.STORAGE_PRIORITIES[0])
        for initial, monthly in ((Decimal("NaN"), None), (Decimal("50"), Decimal("Infinity")),
                                 (50.0, None), (Decimal("-1"), None),
                                 (Decimal("50"), Decimal("0"))):
            with self.subTest(initial=initial, monthly=monthly):
                with self.assertRaises(ValueError):
                    terms.compare_cart(ref, initial_gross=initial, monthly_gross=monthly)

    def test_cli_offline_no_smtp_no_stock_no_cart(self):
        with patch.object(monitor, "retrieve_stock", side_effect=AssertionError("stock")):
            with patch.object(monitor, "send_mail", side_effect=AssertionError("email")):
                self.assertEqual(monitor.main(["--confirmed-terms"]), 0)


if __name__ == "__main__":
    unittest.main()
