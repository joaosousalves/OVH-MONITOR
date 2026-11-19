"""Tests for the safe, non-purchasing OVHcloud Eco cart probe."""

import sys
import hashlib
import json
from io import BytesIO
from unittest.mock import MagicMock
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

    def test_quote_policy_failure_always_deletes_cart(self):
        fake = FakeClient()
        def reject_quote(quote):
            self.assertEqual(quote["prices"]["withTax"]["text"], "39,99 EUR")
            raise probe.OrderProbeError("price policy refused quote")
        with self.assertRaisesRegex(probe.OrderProbeError, "price policy"):
            probe.test_order(fake, quote_validator=reject_quote)
        self.assertEqual(fake.calls[-1][0:2], ("DELETE", "/order/cart/cart-123"))
        self.assertFalse(any(method == "POST" and path.endswith("/checkout")
                             for method, path, _, _ in fake.calls))

    def test_renewal_observer_sees_existing_item_responses_without_extra_calls(self):
        class WithRenewalPrices(FakeClient):
            def request(self, method, path, payload=None, query=None):
                result = super().request(method, path, payload, query)
                if method == "POST" and (path.endswith("/eco") or path.endswith("/eco/options")):
                    result = dict(result)
                    result["prices"] = [{"label": "RENEW", "price": {
                        "currencyCode": "EUR", "value": "9.00",
                    }}]
                return result

        fake = WithRenewalPrices()
        observed = []
        probe.test_order(fake, renewal_observer=observed.append)
        self.assertEqual(len(observed), 1)
        self.assertEqual(len(observed[0]), 4)  # Server + three mandatory addons.
        self.assertFalse(any(method == "GET" and "/item/" in path
                             for method, path, _, _ in fake.calls))
        self.assertEqual(fake.calls[-1][0:2], ("DELETE", "/order/cart/cart-123"))
        self.assertFalse(any(method == "POST" and path.endswith("/checkout")
                             for method, path, _, _ in fake.calls))

    def test_renewal_observer_failure_still_deletes_cart(self):
        fake = FakeClient()
        def fail(_items):
            raise ValueError("simulated evidence failure")
        with self.assertRaisesRegex(ValueError, "evidence failure"):
            probe.test_order(fake, renewal_observer=fail)
        self.assertEqual(fake.calls[-1][0:2], ("DELETE", "/order/cart/cart-123"))

    def test_order_rejection_still_cleans_up_cart(self):
        fake = FakeClient(fail_path="/order/cart/cart-123/eco/options")
        with self.assertRaises(probe.OrderProbeError):
            probe.test_order(fake)
        self.assertEqual(fake.calls[-1][0:2], ("DELETE", "/order/cart/cart-123"))

    def test_missing_monthly_offer_diagnoses_without_exposing_response(self):
        sample = [
            {"planCode": probe.PLAN_CODE, "duration": "P12M",
             "pricingMode": "default", "cartId": "secret-cart-id"},
            {"planCode": "different-plan", "duration": "P1M", "pricingMode": "default"},
        ]
        with self.assertRaises(probe.OrderProbeError) as caught:
            probe.find_monthly_offer(sample)
        message = str(caught.exception)
        self.assertIn("2 offers returned", message)
        self.assertIn("1 match", message)
        self.assertIn("duration=P12M", message)
        self.assertNotIn("secret-cart-id", message)

    def test_matching_offer_diagnostic_exposes_only_field_names(self):
        offer = {
            "planCode": probe.PLAN_CODE,
            "duration": None,
            "pricingMode": None,
            "prices": {"recurring": [{"phase": 1, "interval": 1, "price": 123}]},
            "configuration": [{"name": "foo", "value": "sensitive-account-123"}],
            "secretToken": "really-secret",
        }
        message = probe.offer_diagnostic([offer])
        self.assertIn("top-level=[", message)
        self.assertIn("prices=object(recurring)", message)
        self.assertIn("prices.recurring[0]=object(interval, phase, price)", message)
        self.assertNotIn("sensitive-account-123", message)
        self.assertNotIn("really-secret", message)

    def test_non_matching_offer_schema_not_emitted(self):
        offers = [{"planCode": "other", "secretToken": "do-not-expose"}]
        message = probe.offer_diagnostic(offers)
        self.assertNotIn("Matching offer fields:", message)
        self.assertNotIn("do-not-expose", message)

    def test_empty_eco_offer_list_diagnosis(self):
        with self.assertRaises(probe.OrderProbeError) as caught:
            probe.find_monthly_offer([])
        self.assertIn("0 offers returned", str(caught.exception))
        self.assertIn("Offer samples: none", str(caught.exception))

    def test_does_not_pick_other_plan_or_contract(self):
        with self.assertRaises(probe.OrderProbeError):
            probe.find_monthly_offer([{
                "planCode": probe.PLAN_CODE, "duration": "P12M", "pricingMode": "default"
            }])
        with self.assertRaises(probe.OrderProbeError):
            probe.find_monthly_offer([{
                "planCode": "other-plan", "duration": "P1M", "pricingMode": "default"
            }])

    def test_nested_eco_prices_array_selects_monthly_only(self):
        offers = [{
            "planCode": probe.PLAN_CODE,
            "productName": "KS-LE-6",
            "prices": [
                {"duration": "P12M", "pricingMode": "default", "interval": 12},
                {"duration": "P1M", "pricingMode": "default", "interval": 1},
                {"duration": "P24M", "pricingMode": "default", "interval": 24},
            ],
        }]
        self.assertEqual(
            probe.find_monthly_offer(offers),
            {"planCode": probe.PLAN_CODE, "duration": "P1M", "pricingMode": "default"},
        )

    def test_nested_eco_prices_array_refuses_annual_only(self):
        with self.assertRaises(probe.OrderProbeError):
            probe.find_monthly_offer([{
                "planCode": probe.PLAN_CODE,
                "prices": [{"duration": "P12M", "pricingMode": "default"}],
            }])

    def test_nested_eco_prices_array_refuses_ambiguous_monthly(self):
        with self.assertRaises(probe.OrderProbeError):
            probe.find_monthly_offer([{
                "planCode": probe.PLAN_CODE,
                "prices": [
                    {"duration": "P1M", "pricingMode": "default"},
                    {"duration": "P1M", "pricingMode": "default"},
                ],
            }])

    def test_ks_le_1_cart_uses_own_default_disks_without_checkout_post(self):
        ks_plan = probe.TEST_PLANS["ks-le-1"]
        disk_code = "softraid-3x2000sa-" + ks_plan
        new_plan = {
            "planCode": ks_plan,
            "configurations": CATALOG["plans"][0]["configurations"],
            "addonFamilies": [
                {"name": "storage", "mandatory": True,
                 "addons": [disk_code], "default": disk_code},
                {"name": "memory", "mandatory": True,
                 "addons": ["ram-ks1"], "default": "ram-ks1"},
            ],
        }
        fake = FakeClient(offers=[{
            "planCode": ks_plan,
            "prices": [
                {"duration": "P12M", "pricingMode": "default"},
                {"duration": "P1M", "pricingMode": "default"},
            ],
        }])
        with patch.dict(CATALOG, {"plans": CATALOG["plans"] + [new_plan]}):
            result = probe.test_order(fake, storage_option="default", plan_code=ks_plan)
        self.assertIn(ks_plan, result)
        eco_posts = [body for method, path, body, _ in fake.calls
                     if method == "POST" and path.endswith("/eco")]
        self.assertEqual(len(eco_posts), 1)
        self.assertEqual(eco_posts[0]["planCode"], ks_plan)
        self.assertEqual(eco_posts[0]["duration"], "P1M")
        self.assertTrue(any(method == "POST" and path.endswith("/eco/options")
                            and body["planCode"] == disk_code
                            for method, path, body, _ in fake.calls))
        self.assertEqual(fake.calls[-1][0:2], ("DELETE", "/order/cart/cart-123"))
        self.assertFalse(any(method == "POST" and path.endswith("/checkout")
                             for method, path, _, _ in fake.calls))

    def test_exact_fqn_selects_available_ram_and_disks(self):
        plan = probe.TEST_PLANS["ks-le-1"]
        fqn = plan + ".ram-32g-ddr3-noecc-1333.softraid-3x2000sa"
        disk_ssd = "softraid-2x480ssd-" + plan
        disk_hdd = "softraid-3x2000sa-" + plan
        ram_16 = "ram-16g-ddr3-noecc-1333-" + plan
        ram_32 = "ram-32g-ddr3-noecc-1333-" + plan
        catalog_plan = {
            "planCode": plan,
            "configurations": CATALOG["plans"][0]["configurations"],
            "addonFamilies": [
                {"name": "memory", "mandatory": True,
                 "addons": [ram_16, ram_32], "default": ram_16},
                {"name": "storage", "mandatory": True,
                 "addons": [disk_ssd, disk_hdd], "default": disk_ssd},
            ],
        }
        fake = FakeClient(offers=[{
            "planCode": plan, "prices": [
                {"duration": "P1M", "pricingMode": "default"},
                {"duration": "P12M", "pricingMode": "default"},
            ],
        }])
        with patch.dict(CATALOG, {"plans": CATALOG["plans"] + [catalog_plan]}):
            output = probe.test_order(fake, datacenter="bhs",
                                      plan_code=plan, target_fqn=fqn)
        self.assertIn("total com IVA", output)
        chosen = [body["planCode"] for method, path, body, _ in fake.calls
                  if method == "POST" and path.endswith("/eco/options")]
        self.assertEqual(chosen, [ram_32, disk_hdd])
        self.assertNotIn(ram_16, chosen)
        self.assertNotIn(disk_ssd, chosen)
        self.assertEqual(fake.calls[-1][0:2], ("DELETE", "/order/cart/cart-123"))
        self.assertFalse(any(method == "POST" and path.endswith("/checkout")
                             for method, path, _, _ in fake.calls))

    def test_exact_fqn_rejects_conflicting_storage_override(self):
        plan = probe.TEST_PLANS["ks-le-1"]
        fqn = plan + ".ram-32g-ddr3-noecc-1333.softraid-3x2000sa"
        fake = FakeClient()
        with self.assertRaisesRegex(probe.OrderProbeError, "different --storage"):
            probe.test_order(fake, datacenter="bhs", plan_code=plan,
                             target_fqn=fqn, storage_option="softraid-2x480ssd-" + plan)
        self.assertFalse(fake.calls)  # No catalog call or cart mutation.

    def test_invalid_fqn_rejected_before_cart_creation(self):
        fake = FakeClient()
        with self.assertRaises(probe.OrderProbeError):
            probe.test_order(fake, plan_code=probe.TEST_PLANS["ks-le-1"],
                             target_fqn="27skle01-v1.ram-32g-ddr3-noecc-1333")
        self.assertFalse(fake.calls)

    def test_missing_matching_hardware_addon_rejected_before_cart(self):
        plan = probe.TEST_PLANS["ks-le-1"]
        fqn = plan + ".ram-32g-ddr3-noecc-1333.softraid-3x2000sa"
        fake = FakeClient()
        with self.assertRaises(probe.OrderProbeError):
            probe.test_order(fake, plan_code=plan, target_fqn=fqn)
        self.assertFalse(any(method == "POST" and path == "/order/cart"
                             for method, path, _, _ in fake.calls))

    def test_stock_preflight_rejects_unavailable_without_cart(self):
        import ksle_monitor as monitor
        plan = probe.TEST_PLANS["ks-le-1"]
        fqn = plan + ".ram-32g-ddr3-noecc-1333.softraid-3x2000sa"
        items = [monitor.Stock(fqn, "softraid-3x2000sa", "bhs", "unavailable")]
        with patch.object(monitor, "retrieve_stock", return_value=items) as retrieve:
            with self.assertRaises(probe.OrderProbeError) as caught:
                probe.verify_available_test_fqn(plan, fqn, "bhs")
        retrieve.assert_called_once_with(plan_code=plan)
        self.assertIn("no cart created", str(caught.exception))

    def test_stock_preflight_accepts_exact_available_fqn(self):
        import ksle_monitor as monitor
        plan = probe.TEST_PLANS["ks-le-1"]
        fqn = plan + ".ram-32g-ddr3-noecc-1333.softraid-3x2000sa"
        items = [
            monitor.Stock(fqn, "softraid-3x2000sa", "gra", "unavailable"),
            monitor.Stock(fqn, "softraid-3x2000sa", "bhs", "1H-high"),
        ]
        with patch.object(monitor, "retrieve_stock", return_value=items):
            self.assertIsNone(probe.verify_available_test_fqn(plan, fqn, "bhs"))
            with self.assertRaises(probe.OrderProbeError):
                probe.verify_available_test_fqn(plan, fqn, "gra")

    def test_ks_le_1_requires_explicit_default_storage(self):
        fake = FakeClient()
        with self.assertRaises(probe.OrderProbeError):
            probe.test_order(fake, plan_code=probe.TEST_PLANS["ks-le-1"])
        self.assertFalse(fake.calls)

    def test_unapproved_cart_test_plan_blocked(self):
        fake = FakeClient()
        with self.assertRaises(probe.OrderProbeError):
            probe.test_order(fake, plan_code="unapproved", storage_option="default")
        self.assertFalse(fake.calls)

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

    def test_ak_as_ck_credentials_must_be_complete(self):
        with self.assertRaises(probe.OrderProbeError):
            probe.OVHCartClient(application_key="ak")
        with self.assertRaises(probe.OrderProbeError):
            probe.OVHCartClient("bearer", application_key="ak", application_secret="as", consumer_key="ck")

    def test_signed_headers_include_body_and_query(self):
        client = probe.OVHCartClient(
            application_key="ak", application_secret="as", consumer_key="ck"
        )
        client._clock_offset = 0
        with patch.object(probe.time, "time", return_value=1234567890):
            headers = client._signed_headers(
                "POST", probe.BASE_URL + "/order/cart?ovhSubsidiary=PT", b'{"x":1}'
            )
        canonical = (
            "as+ck+POST+" + probe.BASE_URL +
            '/order/cart?ovhSubsidiary=PT+{"x":1}+1234567890'
        )
        self.assertEqual(headers["X-Ovh-Application"], "ak")
        self.assertEqual(headers["X-Ovh-Consumer"], "ck")
        self.assertEqual(headers["X-Ovh-Timestamp"], "1234567890")
        self.assertEqual(
            headers["X-Ovh-Signature"],
            "$1$" + hashlib.sha1(canonical.encode()).hexdigest()
        )

    def test_signed_request_uses_ovh_server_time_and_never_places_order(self):
        client = probe.OVHCartClient(
            application_key="ak", application_secret="as", consumer_key="ck"
        )
        paths = []
        def fake_open(request, timeout):
            paths.append(request.full_url)
            if request.full_url == probe.TIME_URL:
                return BytesIO(b"1234567890")
            self.assertEqual(request.get_header("X-ovh-application"), "ak")
            self.assertIsNone(request.get_header("Authorization"))
            return BytesIO(b'{"cartId":"cart-123"}')
        with patch.object(probe.time, "time", return_value=1234567890), patch.object(
            probe, "urlopen", side_effect=fake_open
        ):
            self.assertEqual(client.request("POST", "/order/cart", {"ovhSubsidiary": "PT"})["cartId"], "cart-123")
        self.assertEqual(paths, [probe.TIME_URL, probe.BASE_URL + "/order/cart"])

    def test_signed_client_blocks_checkout_post_before_time_request(self):
        client = probe.OVHCartClient(
            application_key="ak", application_secret="as", consumer_key="ck"
        )
        with patch.object(probe, "urlopen") as urlopen:
            with self.assertRaises(probe.OrderProbeError):
                client.request("POST", "/order/cart/cart-123/checkout", {})
            urlopen.assert_not_called()

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
