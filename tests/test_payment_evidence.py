"""All evidence uses offline fake API; no payment or cart is made."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from order_probe import OrderProbeError
from payment_evidence import describe_cart_contracts, inspect_payment_evidence


class FakePaymentClient:
    def __init__(self, method_ids=None, *, status="VALID", oneclick=True, error=None):
        self.method_ids = [42] if method_ids is None else method_ids
        self.status, self.oneclick, self.error = status, oneclick, error
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        if method != "GET":
            raise AssertionError("Non-read-only operation")
        if self.error:
            raise self.error
        if path == "/me/payment/method":
            return self.method_ids
        if path == "/me/payment/method/42":
            return {
                "paymentMethodId": 42, "status": self.status,
                "default": True, "oneclick": self.oneclick,
                "expirationDate": "2099-01-01T00:00:00Z",
                "paymentType": "CREDIT_CARD",
            }
        raise AssertionError("Unexpected GET route")


class PaymentEvidenceTests(unittest.TestCase):
    def test_valid_saved_oneclick_is_not_order_payment_proof(self):
        fake = FakePaymentClient()
        result = inspect_payment_evidence(fake)
        self.assertTrue(result.saved_default_valid)
        self.assertFalse(result.order_payment_verified)
        self.assertIn("order-specific payment eligibility", result.summary())
        self.assertNotIn("42", result.summary())
        self.assertTrue(all(method == "GET" for method, _ in fake.calls))

    def test_absent_payment_method_is_a_safe_no_method_result(self):
        result = inspect_payment_evidence(FakePaymentClient(method_ids=[]))
        self.assertEqual(result.status, "NO_METHOD")
        self.assertFalse(result.saved_default_valid)
        self.assertIn("no saved payment method", result.summary())

    def test_invalid_default_oneclick_is_not_ready(self):
        for status, oneclick in (("EXPIRED", True), ("VALID", False),
                                 ("VALIDATING", None)):
            with self.subTest(status=status, oneclick=oneclick):
                result = inspect_payment_evidence(
                    FakePaymentClient(status=status, oneclick=oneclick)
                )
                self.assertEqual(result.status, "INVALID_DEFAULT")
                self.assertFalse(result.order_payment_verified)

    def test_api_error_exposes_no_method_id_or_raw_response(self):
        fake = FakePaymentClient(error=OrderProbeError(
            "OVHcloud API 403 on GET /me/payment/method/42: private example",
        ))
        result = inspect_payment_evidence(fake)
        self.assertEqual(result.status, "API_UNVERIFIED")
        self.assertNotIn("42", result.summary())
        self.assertNotIn("private example", result.summary())

    def test_contract_references_are_only_counted_and_never_trusted(self):
        result = describe_cart_contracts({"contracts": [
            {"name": "Terms", "url": "https://example.invalid/secret"},
            "https://example.invalid/second",
        ]})
        self.assertIn("2 contract reference", result)
        self.assertIn("NOT VERIFIED", result)
        self.assertNotIn("example.invalid", result)

    def test_missing_or_invalid_contracts_never_prove_commitment(self):
        for obj in (None, {}, {"contracts": None}, {"contracts": []},
                    {"contracts": [99]}, {"contracts": ["x"] * 101}):
            with self.subTest(obj=obj):
                self.assertIn("UNVERIFIED", describe_cart_contracts(obj))


if __name__ == "__main__":
    unittest.main()
