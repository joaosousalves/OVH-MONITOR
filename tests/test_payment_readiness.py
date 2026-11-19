"""Tests for read-only payment readiness; never calls OVHcloud."""
from datetime import datetime, timezone
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import payment_readiness as payments
import order_probe


DETAILS = {
    "paymentMethodId": 42,
    "default": True,
    "status": "VALID",
    "oneclick": True,
    "expirationDate": "2030-11-20T00:00:00Z",
    "paymentType": "CREDIT_CARD",
}


class FakeClient:
    def __init__(self, ids=None, detail=None):
        self.ids = [42] if ids is None else ids
        self.detail = DETAILS.copy() if detail is None else detail
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        if path == "/me/payment/method":
            return self.ids
        if method == "GET" and path.startswith("/me/payment/method/"):
            # Return a deliberately mismatched detail for requested ID 43.
            # Validation, rather than the fake HTTP client, must reject it.
            return self.detail
        raise AssertionError("Unexpected payment operation: " + method + " " + path)


class PaymentReadinessTests(unittest.TestCase):
    def test_valid_default_oneclick_payment(self):
        fake = FakeClient()
        text = payments.payment_readiness(fake)
        self.assertIn("READ-ONLY", text)
        self.assertIn("NOT verified", text)
        self.assertNotIn("42", text)
        self.assertEqual(fake.calls, [
            ("GET", "/me/payment/method"),
            ("GET", "/me/payment/method/42"),
        ])

    def test_no_saved_payment_method(self):
        with self.assertRaises(payments.PaymentReadinessError):
            payments.payment_readiness(FakeClient(ids=[]))

    def test_invalid_default_status_refused(self):
        for status in ("EXPIRED", "PAUSED", "VALIDATING", None, "FAILED"):
            with self.subTest(status=status):
                with self.assertRaises(payments.PaymentReadinessError):
                    payments.payment_readiness(FakeClient(detail={**DETAILS, "status": status}))

    def test_only_valid_oneclick_default_is_accepted(self):
        for modified in ({"default": False}, {"oneclick": False},
                         {"oneclick": None}, {"expirationDate": "2024-01-01T00:00:00Z"}):
            with self.subTest(modified=modified):
                with self.assertRaises(payments.PaymentReadinessError):
                    payments.payment_readiness(FakeClient(detail={**DETAILS, **modified}))

    def test_bad_expiry_is_refused(self):
        for expiry in ("garbage", "2027-01-01", 2027):
            with self.subTest(expiry=expiry):
                with self.assertRaises(payments.PaymentReadinessError):
                    payments.payment_readiness(FakeClient(detail={**DETAILS, "expirationDate": expiry}))

    def test_multiple_defaults_refused(self):
        with self.assertRaises(payments.PaymentReadinessError):
            payments.analyze_payment_methods(
                [42, 43], [DETAILS.copy(), {**DETAILS, "paymentMethodId": 43}],
                now=datetime(2026, 11, 19, tzinfo=timezone.utc)
            )

    def test_id_mismatch_and_duplicates_refused(self):
        with self.assertRaises(payments.PaymentReadinessError):
            payments.payment_readiness(FakeClient(ids=[42, 42]))
        with self.assertRaises(payments.PaymentReadinessError):
            payments.payment_readiness(FakeClient(ids=[43]))

    def test_payment_api_is_read_only_for_cart_client(self):
        client = order_probe.OVHCartClient("token")
        with patch.object(order_probe, "urlopen") as urlopen:
            for path in ("/me/payment/method", "/me/payment/method/42",
                         "/me/order", "/me/order/123", "/me/order/123/status"):
                # GET routes are allowlisted: network is reached when called.
                # Force a harmless mocked connection error.
                urlopen.side_effect = order_probe.URLError("offline test")
                with self.assertRaises(order_probe.OrderProbeError):
                    client.request("GET", path)
            for path in ("/me/payment/method", "/me/order/123/pay",
                         "/order/cart/cart-123/checkout"):
                with self.assertRaises(order_probe.OrderProbeError):
                    client.request("POST", path, {})
            self.assertEqual(urlopen.call_count, 5)


if __name__ == "__main__":
    unittest.main()
