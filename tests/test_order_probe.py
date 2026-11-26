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


class ReadbackClient(FakeClient):
    """Fake structured GET responses; never an actual cart or network."""

    def __init__(self, *, mismatched_dc=False, fail_path=None,
                 ids_only=False, wrong_echo_id=False):
        super().__init__(fail_path=fail_path)
        self.mismatched_dc = mismatched_dc
        self.ids_only = ids_only
        self.wrong_echo_id = wrong_echo_id

    def request(self, method, path, payload=None, query=None):
        result = super().request(method, path, payload, query)
        if method == "GET" and path == "/order/cart/cart-123/item":
            if self.ids_only:
                return list(range(12345, 12349))
            codes = [probe.PLAN_CODE, "bandwidth-500-ks-gen0",
                     "ram-128g-ddr4-ecc-2400-27skle06-v1",
                     probe.PREFERRED_STORAGE_OPTION]
            return [{"itemId": idx, "planCode": code,
                     "duration": "P1M", "pricingMode": "default", "quantity": 1}
                    for idx, code in enumerate(codes, start=12345)]
        if method == "GET" and path.startswith("/order/cart/cart-123/item/") and path.count("/") == 5:
            item_id = int(path.rsplit("/", 1)[1])
            codes = [probe.PLAN_CODE, "bandwidth-500-ks-gen0",
                     "ram-128g-ddr4-ecc-2400-27skle06-v1",
                     probe.PREFERRED_STORAGE_OPTION]
            if 12345 <= item_id <= 12348:
                return {"itemId": item_id + (1 if self.wrong_echo_id else 0),
                        "planCode": codes[item_id - 12345],
                        "duration": "P1M", "pricingMode": "default", "quantity": 1}
        if method == "GET" and path == "/order/cart/cart-123/item/12345/configuration":
            if self.ids_only:
                return [201, 202, 203]
            return [{"label": "dedicated_datacenter",
                     "value": "gra" if self.mismatched_dc else "bhs"},
                    {"label": "region", "value": "canada"},
                    {"label": "dedicated_os", "value": "none_64.en"}]
        if method == "GET" and path.startswith("/order/cart/cart-123/item/12345/configuration/"):
            config_id = int(path.rsplit("/", 1)[1])
            values = {
                201: ("dedicated_datacenter", "bhs"),
                202: ("region", "canada"),
                203: ("dedicated_os", "none_64.en"),
            }
            if config_id in values:
                label, value = values[config_id]
                return {"configurationId": config_id, "label": label, "value": value}
        return result


class OrderProbeTests(unittest.TestCase):
    def test_opt_in_cart_retainer_takes_ownership_only_after_quote(self):
        client = ReadbackClient()
        retainer = MagicMock(return_value=True)
        quote_validator = MagicMock()
        identity = MagicMock()
        fqn = f"{probe.PLAN_CODE}.ram-128g-ddr4-ecc-2400.hybridsoftraid-2x4000sa-2x500nvme"
        probe.test_order(client, target_fqn=fqn, quote_validator=quote_validator,
                         identity_observer=identity, cart_retainer=retainer)
        retainer.assert_called_once_with(cart_id="cart-123", item_id=12345,
                                        fqn=fqn, datacenter="bhs", expire=None)
        self.assertFalse(any(call[0] == "DELETE" for call in client.calls))
        self.assertFalse(any(call[0] == "POST" and call[1].endswith("/checkout")
                             for call in client.calls))

    def test_live_deferred_readback_only_retains_no_checkout(self):
        client = ReadbackClient()
        fqn = f"{probe.PLAN_CODE}.ram-128g-ddr4-ecc-2400.hybridsoftraid-2x4000sa-2x500nvme"
        observer = MagicMock()
        retainer = MagicMock(return_value=True)
        receipts = MagicMock()
        probe.test_order(client, target_fqn=fqn, quote_validator=MagicMock(),
                         identity_observer=observer, cart_retainer=retainer,
                         receipt_observer=receipts, defer_identity_readback=True)
        self.assertEqual(receipts.call_count, 4)
        self.assertEqual(observer.call_args.args[0].status, "UNVERIFIED")
        retainer.assert_called_once()
        self.assertFalse(any(method == "GET" and "/item" in path
                             for method, path, _, _ in client.calls))
        self.assertFalse(any(method == "POST" and path.endswith("/checkout")
                             for method, path, _, _ in client.calls))

    def test_deferred_readback_rejects_without_receipts_and_retainer(self):
        client = FakeClient()
        with self.assertRaises(probe.OrderProbeError):
            probe.test_order(client, defer_identity_readback=True)
        self.assertEqual(client.calls, [])

    def test_retainer_failure_cleans_up_cart(self):
        client = ReadbackClient()
        fqn = f"{probe.PLAN_CODE}.ram-128g-ddr4-ecc-2400.hybridsoftraid-2x4000sa-2x500nvme"
        with self.assertRaises(OSError):
            probe.test_order(client, target_fqn=fqn, quote_validator=MagicMock(),
                             identity_observer=MagicMock(),
                             cart_retainer=MagicMock(side_effect=OSError("disk failure")))
        self.assertTrue(any(call[0] == "DELETE" for call in client.calls))

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

    def test_optional_get_cart_identity_reads_exact_structured_selection(self):
        fake = ReadbackClient()
        fqn = ("27skle06-v1.ram-128g-ddr4-ecc-2400."
               "hybridsoftraid-2x4000sa-2x500nvme")
        observed = []
        probe.test_order(fake, target_fqn=fqn, identity_observer=observed.append)
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0].verified)
        self.assertIn(("GET", "/order/cart/cart-123/item", None, None), fake.calls)
        self.assertIn(("GET", "/order/cart/cart-123/item/12345/configuration", None, None), fake.calls)
        self.assertEqual(fake.calls[-1][0:2], ("DELETE", "/order/cart/cart-123"))

    def test_schema_observer_uses_existing_cart_calls_and_get_only_readback(self):
        from cart_schema_diagnostic import SchemaTrace
        fake = ReadbackClient(ids_only=True)
        fqn = ("27skle06-v1.ram-128g-ddr4-ecc-2400."
               "hybridsoftraid-2x4000sa-2x500nvme")
        trace = SchemaTrace()
        identity = []
        probe.test_order(fake, target_fqn=fqn, identity_observer=identity.append,
                         schema_observer=trace.capture)
        self.assertTrue(identity[0].verified)
        report = trace.report()
        self.assertIn("base_item: ", report)
        self.assertIn("addon_item: ", report)
        self.assertIn("checkout: ", report)
        self.assertIn("items_get: list(4); sampled=4 [number=4]", report)
        self.assertIn("items_expanded: list(4); sampled=4 [object=4]", report)
        self.assertIn("configurations_get: list(3)", report)
        self.assertIn("configurations_expanded: list(3)", report)
        self.assertNotIn("12345", report)
        self.assertEqual(fake.calls[-1][:2], ("DELETE", "/order/cart/cart-123"))
        self.assertFalse(any(method == "POST" and (path.endswith("/checkout") or "/pay" in path)
                             for method, path, _, _ in fake.calls))

    def test_schema_observer_get_denied_does_not_leak_exception(self):
        from cart_schema_diagnostic import SchemaTrace
        fake = ReadbackClient(fail_path="/order/cart/cart-123/item")
        fqn = ("27skle06-v1.ram-128g-ddr4-ecc-2400."
               "hybridsoftraid-2x4000sa-2x500nvme")
        trace = SchemaTrace()
        identity = []
        probe.test_order(fake, target_fqn=fqn, identity_observer=identity.append,
                         schema_observer=trace.capture)
        self.assertEqual(identity[0].status, "UNVERIFIED")
        self.assertIn("readback_unavailable", trace.report())
        self.assertNotIn("cart-123", trace.report())
        self.assertEqual(fake.calls[-1][:2], ("DELETE", "/order/cart/cart-123"))

    def test_schema_cli_probe_report_requires_stock_and_cleanup(self):
        fake = ReadbackClient()
        fqn = ("27skle06-v1.ram-128g-ddr4-ecc-2400."
               "hybridsoftraid-2x4000sa-2x500nvme")
        with patch.object(probe, "verify_available_test_fqn") as stock:
            with patch.object(probe, "OVHCartClient", return_value=fake):
                with patch.dict(probe.os.environ, {"OVH_TOKEN": "offline-test"}):
                    with patch("builtins.print") as output:
                        rc = probe.run_test_order("bhs", "preferred", "ks-le-6", fqn,
                                                  schema=True)
        self.assertEqual(rc, 0)
        stock.assert_called_once_with(probe.PLAN_CODE, fqn, "bhs")
        report = output.call_args_list[-1].args[0]
        self.assertIn("REDACTED MANUAL CART SCHEMA", report)
        self.assertIn("prices=missing", report)
        self.assertIn("configurations_expanded:", report)
        self.assertNotIn("cart-123", report)
        self.assertIn("raw item details and price display omitted", output.call_args_list[0].args[0])
        self.assertEqual(fake.calls[-1][:2], ("DELETE", "/order/cart/cart-123"))
        self.assertFalse(any(method == "POST" and (path.endswith("/checkout") or "/pay" in path)
                             for method, path, _, _ in fake.calls))

    def test_optional_identity_get_denied_remains_unverified_and_cleans_cart(self):
        fake = ReadbackClient(fail_path="/order/cart/cart-123/item")
        fqn = ("27skle06-v1.ram-128g-ddr4-ecc-2400."
               "hybridsoftraid-2x4000sa-2x500nvme")
        observed = []
        probe.test_order(fake, target_fqn=fqn, identity_observer=observed.append)
        self.assertEqual(observed[0].status, "UNVERIFIED")
        self.assertEqual(fake.calls[-1][0:2], ("DELETE", "/order/cart/cart-123"))

    def test_optional_cart_identity_conflict_does_not_approve_selected_dc(self):
        fake = ReadbackClient(mismatched_dc=True)
        fqn = ("27skle06-v1.ram-128g-ddr4-ecc-2400."
               "hybridsoftraid-2x4000sa-2x500nvme")
        observed = []
        probe.test_order(fake, target_fqn=fqn, identity_observer=observed.append)
        self.assertEqual(observed[0].status, "CONFLICT")
        self.assertEqual(fake.calls[-1][0:2], ("DELETE", "/order/cart/cart-123"))

    def test_cart_readback_can_follow_listed_ids_with_get_only(self):
        fake = ReadbackClient(ids_only=True)
        fqn = ("27skle06-v1.ram-128g-ddr4-ecc-2400."
               "hybridsoftraid-2x4000sa-2x500nvme")
        observed = []
        probe.test_order(fake, target_fqn=fqn, identity_observer=observed.append)
        self.assertTrue(observed[0].verified)
        self.assertIn(("GET", "/order/cart/cart-123/item/12346", None, None), fake.calls)
        self.assertIn(("GET", "/order/cart/cart-123/item/12345/configuration/201", None, None), fake.calls)
        self.assertEqual(fake.calls[-1][0:2], ("DELETE", "/order/cart/cart-123"))
        self.assertFalse(any(method == "POST" and path.endswith("/checkout")
                             for method, path, _, _ in fake.calls))

    def test_inconsistent_get_item_identifier_is_conflict_and_cart_deleted(self):
        fake = ReadbackClient(ids_only=True, wrong_echo_id=True)
        fqn = ("27skle06-v1.ram-128g-ddr4-ecc-2400."
               "hybridsoftraid-2x4000sa-2x500nvme")
        observed = []
        probe.test_order(fake, target_fqn=fqn, identity_observer=observed.append)
        self.assertEqual(observed[0].status, "CONFLICT")
        self.assertEqual(fake.calls[-1][0:2], ("DELETE", "/order/cart/cart-123"))

    def test_cart_checkout_contract_with_twelve_month_term_fails_closed(self):
        class LongCommitment(FakeClient):
            def request(self, method, path, payload=None, query=None):
                result = super().request(method, path, payload, query)
                if method == "GET" and path.endswith("/checkout"):
                    result["contracts"] = [{"commitmentDuration": "P12M"}]
                return result
        fake = LongCommitment()
        with self.assertRaisesRegex(probe.OrderProbeError, "Fixed-term"):
            probe.test_order(fake)
        self.assertEqual(fake.calls[-1][0:2], ("DELETE", "/order/cart/cart-123"))
        self.assertFalse(any(method == "POST" and path.endswith("/checkout")
                             for method, path, _, _ in fake.calls))

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

    def test_ks_le_3_requires_exact_stock_fqn_before_any_api_calls(self):
        fake = FakeClient()
        with self.assertRaisesRegex(probe.OrderProbeError, "exact --test-fqn"):
            probe.test_order(fake, plan_code=probe.TEST_PLANS["ks-le-3"],
                             storage_option="default")
        self.assertFalse(fake.calls)
        with patch.object(probe, "OVHCartClient") as client:
            self.assertEqual(probe.run_test_order("bhs", "default", "ks-le-3"), 1)
        client.assert_not_called()

    def test_ks_le_3_available_exact_fqn_uses_p1m_and_deletes_test_cart(self):
        plan = probe.TEST_PLANS["ks-le-3"]
        memory = "ram-32g-ddr4-ecc-2666"
        storage = "softraid-2x4000sa"
        fqn = f"{plan}.{memory}.{storage}"
        catalog_plan = {
            "planCode": plan,
            "configurations": CATALOG["plans"][0]["configurations"],
            "addonFamilies": [
                {"name": "memory", "mandatory": True,
                 "addons": [f"{memory}-{plan}"], "default": f"{memory}-{plan}"},
                {"name": "storage", "mandatory": True,
                 "addons": [f"{storage}-{plan}"], "default": f"{storage}-{plan}"},
            ],
        }
        fake = FakeClient(offers=[{
            "planCode": plan, "prices": [
                {"duration": "P12M", "pricingMode": "default"},
                {"duration": "P1M", "pricingMode": "default"},
            ],
        }])
        with patch.dict(CATALOG, {"plans": CATALOG["plans"] + [catalog_plan]}):
            with patch.object(probe, "verify_available_test_fqn") as stock_check:
                with patch.object(probe, "OVHCartClient", return_value=fake):
                    with patch.dict(probe.os.environ, {"OVH_TOKEN": "offline-test"}):
                        with patch("builtins.print") as printed:
                            rc = probe.run_test_order("bhs", "preferred", "ks-le-3", fqn)
        self.assertEqual(rc, 0)
        stock_check.assert_called_once_with(plan, fqn, "bhs")
        self.assertIn(plan, printed.call_args.args[0])
        posts = [(path, body) for method, path, body, _ in fake.calls if method == "POST"]
        base = [body for path, body in posts if path.endswith("/eco")]
        self.assertEqual(len(base), 1)
        self.assertEqual(base[0]["planCode"], plan)
        self.assertEqual(base[0]["duration"], "P1M")
        self.assertEqual([body["planCode"] for path, body in posts
                          if path.endswith("/eco/options")],
                         [f"{memory}-{plan}", f"{storage}-{plan}"])
        self.assertFalse(any(path.endswith("/checkout") for path, _ in posts))
        self.assertEqual(fake.calls[-1][:2], ("DELETE", "/order/cart/cart-123"))

    def test_ks_le_3_unavailable_fqn_never_creates_cart(self):
        import ksle_monitor as monitor
        plan = probe.TEST_PLANS["ks-le-3"]
        fqn = plan + ".ram-32g-ddr4-ecc-2666.softraid-2x4000sa"
        unavailable = monitor.Stock(fqn, "softraid-2x4000sa", "gra", "unavailable")
        with patch.object(monitor, "retrieve_stock", return_value=[unavailable]):
            with patch.object(probe, "OVHCartClient") as client:
                self.assertEqual(probe.run_test_order("gra", "preferred", "ks-le-3", fqn), 1)
        client.assert_not_called()

    def test_ks_le_2_exact_stock_uses_p1m_and_does_not_submit_checkout(self):
        plan = probe.TEST_PLANS["ks-le-2"]
        memory = "ram-32g-ddr3-ecc-1333"
        storage = "softraid-2x2000sa"
        fqn = f"{plan}.{memory}.{storage}"
        catalog_plan = {
            "planCode": plan,
            "configurations": CATALOG["plans"][0]["configurations"],
            "addonFamilies": [
                {"name": "memory", "mandatory": True,
                 "addons": [f"{memory}-{plan}"], "default": f"{memory}-{plan}"},
                {"name": "storage", "mandatory": True,
                 "addons": [f"{storage}-{plan}"], "default": f"{storage}-{plan}"},
            ],
        }
        fake = FakeClient(offers=[{
            "planCode": plan, "prices": [
                {"duration": "P12M", "pricingMode": "default"},
                {"duration": "P1M", "pricingMode": "default"},
            ],
        }])
        with patch.dict(CATALOG, {"plans": CATALOG["plans"] + [catalog_plan]}):
            with patch.object(probe, "verify_available_test_fqn") as preflight:
                with patch.object(probe, "OVHCartClient", return_value=fake):
                    with patch.dict(probe.os.environ, {"OVH_TOKEN": "offline-test"}):
                        with patch("builtins.print") as printed:
                            rc = probe.run_test_order("gra", "preferred", "ks-le-2", fqn)
        self.assertEqual(rc, 0)
        preflight.assert_called_once_with(plan, fqn, "gra")
        self.assertIn(plan, printed.call_args.args[0])
        posts = [(path, body) for method, path, body, _ in fake.calls if method == "POST"]
        base = [body for path, body in posts if path.endswith("/eco")]
        self.assertEqual(len(base), 1)
        self.assertEqual(base[0]["planCode"], plan)
        self.assertEqual(base[0]["duration"], "P1M")
        self.assertEqual([body["planCode"] for path, body in posts
                          if path.endswith("/eco/options")],
                         [f"{memory}-{plan}", f"{storage}-{plan}"])
        self.assertFalse(any(path.endswith("/checkout") for path, _ in posts))
        self.assertEqual(fake.calls[-1][:2], ("DELETE", "/order/cart/cart-123"))

    def test_ks_le_2_detailed_quote_uses_get_readback_and_redacted_report(self):
        plan = probe.TEST_PLANS["ks-le-2"]
        memory = "ram-32g-ddr3-ecc-1333"
        storage = "softraid-2x2000sa"
        fqn = f"{plan}.{memory}.{storage}"
        codes = [plan, "bandwidth-500-ks-gen0", f"{memory}-{plan}", f"{storage}-{plan}"]
        catalog_plan = {
            "planCode": plan,
            "configurations": CATALOG["plans"][0]["configurations"],
            "addonFamilies": [
                {"name": "bandwidth", "mandatory": True,
                 "addons": [codes[1]], "default": codes[1]},
                {"name": "memory", "mandatory": True,
                 "addons": [codes[2]], "default": codes[2]},
                {"name": "storage", "mandatory": True,
                 "addons": [codes[3]], "default": codes[3]},
            ],
        }

        class DetailedFake(FakeClient):
            def request(self, method, path, payload=None, query=None):
                result = super().request(method, path, payload, query)
                if method == "GET" and path == "/order/cart/cart-123/item":
                    return [
                        {"planCode": code, "itemId": idx, "duration": "P1M",
                         "pricingMode": "default", "quantity": 1}
                        for idx, code in enumerate(codes, 12345)
                    ]
                if method == "GET" and path == "/order/cart/cart-123/item/12345/configuration":
                    return [
                        {"label": "dedicated_datacenter", "value": "gra"},
                        {"label": "region", "value": "europe"},
                        {"label": "dedicated_os", "value": "none_64.en"},
                    ]
                if method == "GET" and path.endswith("/checkout"):
                    return {
                        "prices": {
                            "withoutTax": {"value": "33.98", "currencyCode": "EUR"},
                            "tax": {"value": "7.82", "currencyCode": "EUR"},
                            "withTax": {"value": "41.80", "currencyCode": "EUR",
                                        "text": "41.80 EUR"},
                        },
                        "details": [{"detailType": "INSTALLATION", "totalPrice": {
                            "currencyCode": "EUR", "value": "10.00"},
                            "description": "PRIVATE-CUSTOMER-ADDRESS"}],
                        "commitmentDuration": "P0M",
                    }
                if method == "POST" and (path.endswith("/eco") or path.endswith("/eco/options")):
                    result = dict(result)
                    result["prices"] = [{"label": "RENEW",
                                         "price": {"currencyCode": "EUR", "value": "5.00"},
                                         "priceWithTax": {"currencyCode": "EUR", "value": "6.15"}}]
                    result["duration"] = "P1M"
                return result

        fake = DetailedFake(offers=[{
            "planCode": plan, "prices": [{"duration": "P1M", "pricingMode": "default"}],
        }])
        with patch.dict(CATALOG, {"plans": CATALOG["plans"] + [catalog_plan]}):
            with patch.object(probe, "verify_available_test_fqn") as verify:
                with patch.object(probe, "OVHCartClient", return_value=fake):
                    with patch.dict(probe.os.environ, {"OVH_TOKEN": "offline-test"}):
                        with patch("builtins.print") as output:
                            rc = probe.run_test_order("gra", "preferred", "ks-le-2", fqn,
                                                      detailed=True)
        self.assertEqual(rc, 0)
        verify.assert_called_once_with(plan, fqn, "gra")
        report = output.call_args_list[-1].args[0]
        self.assertIn("INITIAL VAT: 33.98 EUR net", report)
        self.assertIn("ITEM RENEW WITH VAT: 24.60 EUR", report)
        self.assertIn("CART IDENTITY VERIFIED", report)
        self.assertNotIn("PRIVATE-CUSTOMER-ADDRESS", report)
        self.assertEqual(fake.calls[-1][:2], ("DELETE", "/order/cart/cart-123"))
        self.assertFalse(any(method == "POST" and path.endswith("/checkout")
                             for method, path, _, _ in fake.calls))

    def test_detailed_quote_missing_fields_is_unverified_and_cleans_cart(self):
        fake = FakeClient()
        fqn = ("27skle06-v1.ram-128g-ddr4-ecc-2400."
               "hybridsoftraid-2x4000sa-2x500nvme")
        with patch.object(probe, "verify_available_test_fqn"):
            with patch.object(probe, "OVHCartClient", return_value=fake):
                with patch.dict(probe.os.environ, {"OVH_TOKEN": "offline-test"}):
                    with patch("builtins.print") as output:
                        rc = probe.run_test_order("bhs", "preferred", "ks-le-6", fqn,
                                                  detailed=True)
        self.assertEqual(rc, 0)
        report = output.call_args_list[-1].args[0]
        self.assertIn("INITIAL VAT: UNVERIFIED", report)
        self.assertIn("ITEM RENEW WITH VAT: UNVERIFIED", report)
        self.assertIn("CART IDENTITY UNVERIFIED", report)
        self.assertEqual(fake.calls[-1][:2], ("DELETE", "/order/cart/cart-123"))

    def test_detailed_quote_cart_identity_conflict_is_failure_after_cleanup(self):
        fake = ReadbackClient(mismatched_dc=True)
        fqn = ("27skle06-v1.ram-128g-ddr4-ecc-2400."
               "hybridsoftraid-2x4000sa-2x500nvme")
        with patch.object(probe, "verify_available_test_fqn"):
            with patch.object(probe, "OVHCartClient", return_value=fake):
                with patch.dict(probe.os.environ, {"OVH_TOKEN": "offline-test"}):
                    with patch("builtins.print") as output:
                        rc = probe.run_test_order("bhs", "preferred", "ks-le-6", fqn,
                                                  detailed=True)
        self.assertEqual(rc, 1)
        self.assertIn("CART IDENTITY CONFLICT", output.call_args_list[-1].args[0])
        self.assertEqual(fake.calls[-1][:2], ("DELETE", "/order/cart/cart-123"))

    def test_detailed_quote_addon_non_monthly_blocks_and_cleans_cart(self):
        class WrongDuration(FakeClient):
            def request(self, method, path, payload=None, query=None):
                result = super().request(method, path, payload, query)
                if method == "POST" and path.endswith("/eco/options"):
                    result = dict(result)
                    result["duration"] = "P12M"
                return result

        fake = WrongDuration()
        fqn = ("27skle06-v1.ram-128g-ddr4-ecc-2400."
               "hybridsoftraid-2x4000sa-2x500nvme")
        with patch.object(probe, "verify_available_test_fqn"):
            with patch.object(probe, "OVHCartClient", return_value=fake):
                with patch.dict(probe.os.environ, {"OVH_TOKEN": "offline-test"}):
                    self.assertEqual(probe.run_test_order("bhs", "preferred", "ks-le-6", fqn,
                                                          detailed=True), 1)
        self.assertEqual(fake.calls[-1][:2], ("DELETE", "/order/cart/cart-123"))

    def test_ks_le_2_requires_fqn_and_read_only_catalog_match(self):
        plan = probe.TEST_PLANS["ks-le-2"]
        fqn = f"{plan}.ram-32g-ddr3-ecc-1333.softraid-2x2000sa"
        with patch.object(probe, "OVHCartClient") as client:
            self.assertEqual(probe.run_test_order("gra", "preferred", "ks-le-2"), 1)
        client.assert_not_called()

        catalog_plan = {
            "planCode": plan,
            "configurations": CATALOG["plans"][0]["configurations"],
            "addonFamilies": [
                {"name": "memory", "mandatory": True,
                 "addons": [f"ram-32g-ddr3-ecc-1333-{plan}"],
                 "default": f"ram-32g-ddr3-ecc-1333-{plan}"},
                {"name": "storage", "mandatory": True,
                 "addons": [f"softraid-2x2000sa-{plan}"],
                 "default": f"softraid-2x2000sa-{plan}"},
            ],
        }
        with patch.dict(CATALOG, {"plans": CATALOG["plans"] + [catalog_plan]}):
            with patch.object(probe, "verify_available_test_fqn") as stock:
                with patch("pricing.fetch_catalog", return_value=CATALOG) as catalog:
                    with patch("builtins.print") as output:
                        self.assertEqual(probe.run_check_test_config("gra", "ks-le-2", fqn), 0)
        stock.assert_called_once_with(plan, fqn, "gra")
        catalog.assert_called_once()
        self.assertIn("PUBLIC PREFLIGHT OK", output.call_args.args[0])

    def test_ks_4_exact_stock_catalog_match_and_monthly_test_cart(self):
        plan = probe.TEST_PLANS["ks-4"]
        memory = "ram-32g-ecc-2133"
        storage = "softraid-2x450nvme"
        fqn = f"{plan}.{memory}.{storage}"
        catalog_plan = {
            "planCode": plan,
            "configurations": CATALOG["plans"][0]["configurations"],
            "addonFamilies": [
                {"name": "memory", "mandatory": True,
                 "addons": [f"{memory}-{plan}"], "default": f"{memory}-{plan}"},
                {"name": "storage", "mandatory": True,
                 "addons": [f"{storage}-{plan}"], "default": f"{storage}-{plan}"},
            ],
        }
        fake = FakeClient(offers=[{"planCode": plan, "prices": [
            {"duration": "P12M", "pricingMode": "default"},
            {"duration": "P1M", "pricingMode": "default"},
        ]}])
        with patch.dict(CATALOG, {"plans": CATALOG["plans"] + [catalog_plan]}):
            with patch.object(probe, "verify_available_test_fqn") as preflight:
                with patch("pricing.fetch_catalog", return_value=CATALOG):
                    with patch("builtins.print") as output:
                        self.assertEqual(probe.run_check_test_config("bhs", "ks-4", fqn), 0)
                with patch.object(probe, "OVHCartClient", return_value=fake):
                    with patch.dict(probe.os.environ, {"OVH_TOKEN": "offline-test"}):
                        self.assertEqual(probe.run_test_order("bhs", "preferred", "ks-4", fqn), 0)
        self.assertEqual(preflight.call_count, 2)
        self.assertIn("PUBLIC PREFLIGHT OK", output.call_args.args[0])
        post_eco = [body for method, path, body, _ in fake.calls
                    if method == "POST" and path.endswith("/eco")]
        self.assertEqual(len(post_eco), 1)
        self.assertEqual(post_eco[0]["planCode"], plan)
        self.assertEqual(post_eco[0]["duration"], "P1M")
        self.assertFalse(any(method == "POST" and path.endswith("/checkout")
                             for method, path, _, _ in fake.calls))
        self.assertEqual(fake.calls[-1][:2], ("DELETE", "/order/cart/cart-123"))

    def test_ks_4_no_exact_fqn_or_missing_catalog_addon_blocks_cart(self):
        fake = FakeClient()
        plan = probe.TEST_PLANS["ks-4"]
        with self.assertRaisesRegex(probe.OrderProbeError, "exact --test-fqn"):
            probe.test_order(fake, plan_code=plan, storage_option="default")
        self.assertFalse(fake.calls)
        fqn = plan + ".ram-32g-ecc-2133.softraid-2x450nvme"
        with self.assertRaisesRegex(probe.OrderProbeError, "not in OVHcloud's PT catalog"):
            probe.test_order(fake, plan_code=plan, target_fqn=fqn)
        self.assertFalse(any(method == "POST" and path == "/order/cart"
                             for method, path, _, _ in fake.calls))

    def test_ks_le_3_stock_catalog_memory_mismatch_fails_read_only(self):
        plan = probe.TEST_PLANS["ks-le-3"]
        fqn = f"{plan}.ram-64g-ddr4-ecc-2666.softraid-3x4000sa"
        catalog_plan = {
            "planCode": plan,
            "configurations": CATALOG["plans"][0]["configurations"],
            "addonFamilies": [
                {"name": "memory", "mandatory": True,
                 "addons": [f"ram-32g-ddr4-ecc-2666-{plan}"],
                 "default": f"ram-32g-ddr4-ecc-2666-{plan}"},
                {"name": "storage", "mandatory": True,
                 "addons": [f"softraid-3x4000sa-{plan}"],
                 "default": f"softraid-3x4000sa-{plan}"},
            ],
        }
        with patch.dict(CATALOG, {"plans": CATALOG["plans"] + [catalog_plan]}):
            with patch.object(probe, "verify_available_test_fqn"):
                with patch("pricing.fetch_catalog", return_value=CATALOG):
                    with patch.object(probe, "OVHCartClient") as client:
                        self.assertEqual(probe.run_check_test_config("gra", "ks-le-3", fqn), 1)
        client.assert_not_called()

    def test_read_only_config_check_requires_exact_fqn(self):
        with patch("pricing.fetch_catalog") as fetch:
            self.assertEqual(probe.run_check_test_config("gra", "ks-le-2", None), 1)
        fetch.assert_not_called()

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
