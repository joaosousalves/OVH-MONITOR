"""Tests for the safe, non-purchasing OVHcloud Eco cart probe."""

import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import order_probe as probe


CATALOG = {
    "plans": [
        {
            "planCode": probe.PLAN_CODE,
            "configurations": [
                {"name": "dedicated_datacenter", "values": ["bhs", "gra"]},
                {"name": "dedicated_os", "values": ["none_64.en"]},
                {"name": "region", "values": ["canada", "europe"]},
            ],
            "addonFamilies": [
                {"name": "bandwidth", "mandatory": True, "addons": ["bandwidth-500-ks-gen0"], "default": "bandwidth-500-ks-gen0"},
                {"name": "memory", "mandatory": True, "addons": ["ram-128g-ddr4-ecc-2400-27skle06-v1"], "default": "ram-128g-ddr4-ecc-2400-27skle06-v1"},
                {"name": "storage", "mandatory": True, "addons": [probe.PREFERRED_STORAGE_OPTION], "default": probe.PREFERRED_STORAGE_OPTION},
            ],
        },
    ]
}


class FakeClient:
    def __init__(self, offers=None, fail_path=None):
        self.calls = []
        self.offers = offers if offers is not None else [
            {"planCode": probe.PLAN_CODE, "duration": "P1M", "pricingMode": "default"}
        ]
        self.fail_path = fail_path

    def request(self, method, path, payload=None, query=None):
        self.calls.append((method, path, payload, query))
        if path == self.fail_path:
            raise probe.OrderProbeError("simulated rejection")
        if path == "/order/catalog/public/eco":
            return CATALOG
        if method == "POST" and path == "/order/cart":
            return {"cartId": "cart-123"}
        if path == "/order/cart/cart-123/eco" and method == "GET":
            return self.offers
        if path == "/order/cart/cart-123/eco" and method == "POST":
            return {"itemId": 12345}
        if path.endswith("/checkout") and method == "GET":
            return {"prices": {"withTax": {"text": "39,99 EUR"}}}
        return {}


class OrderProbeTests(unittest.TestCase):
    def test_catalog_preferred_disk_and_bhs(self):
        configs, addons = probe.catalog_selection(CATALOG, probe.PREFERRED_STORAGE_OPTION, "bhs")
        self.assertEqual(configs["region"], "canada")
        self.assertEqual(len(addons), 3)
        self.assertIn(probe.PREFERRED_STORAGE_OPTION, addons)

    def test_catalog_other_datacenter(self):
        configs, _ = probe.catalog_selection(CATALOG, probe.PREFERRED_STORAGE_OPTION, "gra")
        self.assertEqual(configs["region"], "europe")

    def test_catalog_invalid_storage_refused_before_cart(self):
        fake = FakeClient()
        with self.assertRaises(probe.OrderProbeError):
            probe.test_order(fake, storage_option="some-other-storage")
        self.assertFalse(any(path == "/order/cart" for _, path, _, _ in fake.calls))

    def test_successful_cart_quote_never_places_order(self):
        fake = FakeClient()
        result = probe.test_order(fake)
        self.assertIn("39,99 EUR", result)
        self.assertEqual(sum(method == "POST" and path == "/order/cart" for method, path, _, _ in fake.calls), 1)
        self.assertIn(("GET", "/order/cart/cart-123/checkout", None, None), fake.calls)
        self.assertEqual(fake.calls[-1][0:2], ("DELETE", "/order/cart/cart-123"))
        self.assertFalse(any(method == "POST" and path.endswith("/checkout") for method, path, _, _ in fake.calls))
        opts = [body["planCode"] for method, path, body, _ in fake.calls if path.endswith("/eco/options")]
        self.assertEqual(len(opts), 3)
        self.assertIn(probe.PREFERRED_STORAGE_OPTION, opts)

    def test_order_rejection_still_cleans_up_cart(self):
        fake = FakeClient(fail_path="/order/cart/cart-123/eco/options")
        with self.assertRaises(probe.OrderProbeError):
            probe.test_order(fake)
        self.assertEqual(fake.calls[-1][0:2], ("DELETE", "/order/cart/cart-123"))

    def test_no_monthly_offer_cleans_up_cart(self):
        fake = FakeClient(offers=[])
        with self.assertRaises(probe.OrderProbeError):
            probe.test_order(fake)
        self.assertEqual(fake.calls[-1][0], "DELETE")

    def test_checkout_post_and_payment_paths_always_blocked(self):
        client = probe.OVHCartClient("test-token")
        with patch.object(probe, "urlopen") as urlopen:
            with self.assertRaises(probe.OrderProbeError):
                client.request("POST", "/order/cart/cart-123/checkout", {})
            with self.assertRaises(probe.OrderProbeError):
                client.request("POST", "/me/order/123/pay", {})
            urlopen.assert_not_called()

    def test_token_is_required(self):
        with self.assertRaises(probe.OrderProbeError):
            probe.OVHCartClient("")

    def test_pt_only(self):
        fake = FakeClient()
        with self.assertRaises(probe.OrderProbeError):
            probe.test_order(fake, subsidiary="US")
        self.assertFalse(fake.calls)

    def test_unexpected_quote_is_refused(self):
        with self.assertRaises(probe.OrderProbeError):
            probe.extract_quote({"prices": {}})


if __name__ == "__main__":
    unittest.main()
