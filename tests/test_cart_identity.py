"""Pure, conservative validation of an Eco cart's GET-only readback."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cart_identity as identity
from purchase_guard import APPROVED_FQNS


ADDONS = (
    "bandwidth-500-ks-gen0",
    "ram-128g-ddr4-ecc-2400-27skle06-v1",
    "hybridsoftraid-2x4000sa-2x500nvme-27skle06-v1",
)


def fixture():
    items = []
    for idx, code in enumerate(("27skle06-v1", *ADDONS), 1):
        items.append({
            "planCode": code, "itemId": idx,
            "duration": "P1M", "pricingMode": "default", "quantity": 1,
        })
    configs = [
        {"label": "dedicated_datacenter", "value": "bhs"},
        {"label": "region", "value": "canada"},
        {"label": "dedicated_os", "value": "none_64.en"},
    ]
    params = dict(
        fqn=APPROVED_FQNS[0], datacenter="bhs", base_item_id=1,
        addons=list(ADDONS),
        configurations={row["label"]: row["value"] for row in configs},
        cart_items=items, cart_configurations=configs,
    )
    return params


class CartIdentityTests(unittest.TestCase):
    def test_complete_readback_matches_exact_model_and_datacenter(self):
        result = identity.inspect_cart_identity(**fixture())
        self.assertTrue(result.verified)
        self.assertIn("VERIFIED", result.summary())

    def test_missing_or_incomplete_readback_is_not_proof(self):
        for replace in (
            {"cart_items": None},
            {"cart_items": []},
            {"cart_configurations": []},
            {"cart_configurations": [{}]},
            {"cart_items": [{"itemId": 1}]},
            {"cart_items": [{"planCode": "27skle06-v1"}]},
        ):
            params = fixture()
            params.update(replace)
            with self.subTest(replace=replace):
                self.assertEqual(identity.inspect_cart_identity(**params).status, "UNVERIFIED")

    def test_wrong_hardware_location_or_term_is_conflict(self):
        mutations = (
            ("cart_items", 0, "planCode", "27skle01-v1"),
            ("cart_items", 1, "planCode", "ram-64g"),
            ("cart_items", 0, "duration", "P12M"),
            ("cart_items", 0, "pricingMode", "upfront"),
            ("cart_items", 0, "quantity", 2),
            ("cart_items", 0, "itemId", 9999),
            ("cart_configurations", 0, "value", "gra"),
            ("cart_configurations", 1, "value", "europe"),
        )
        for section, index, key, value in mutations:
            params = fixture()
            params[section][index][key] = value
            with self.subTest(section=section, key=key, value=value):
                result = identity.inspect_cart_identity(**params)
                self.assertEqual(result.status, "CONFLICT")
                self.assertFalse(result.verified)

    def test_unknown_extra_addon_or_config_blocks_identity(self):
        params = fixture()
        params["cart_items"].append({
            "planCode": "unknown-premium", "itemId": 999,
            "duration": "P1M", "pricingMode": "default", "quantity": 1,
        })
        self.assertEqual(identity.inspect_cart_identity(**params).status, "CONFLICT")
        params = fixture()
        params["cart_configurations"].append({"label": "extra", "value": "sensitive"})
        self.assertEqual(identity.inspect_cart_identity(**params).status, "CONFLICT")

    def test_requested_fqn_and_mandatory_addons_must_agree(self):
        params = fixture()
        params["fqn"] = APPROVED_FQNS[1]
        self.assertEqual(identity.inspect_cart_identity(**params).status, "CONFLICT")
        params = fixture()
        params["addons"] = [ADDONS[0], ADDONS[1]]
        self.assertEqual(identity.inspect_cart_identity(**params).status, "CONFLICT")
        params = fixture()
        params["addons"] = [ADDONS[0], {"bad": "unhashable"}]
        self.assertEqual(identity.inspect_cart_identity(**params).status, "CONFLICT")

    def test_bounded_and_redacted_report(self):
        params = fixture()
        params["cart_items"][0]["secret"] = "secret-cart-token-123"
        result = identity.inspect_cart_identity(**params)
        self.assertTrue(result.verified)
        self.assertNotIn("secret-cart-token", result.summary())
        params["cart_items"] *= 30
        self.assertEqual(identity.inspect_cart_identity(**params).status, "UNVERIFIED")

    def test_manual_ks_le_2_identity_verifies_exact_catalog_codes(self):
        plan = "27skle02-v1"
        fqn = plan + ".ram-32g-ddr3-ecc-1333.softraid-2x2000sa"
        addons = [
            "bandwidth-500-ks-gen0",
            "ram-32g-ddr3-ecc-1333-27skle02-v1",
            "softraid-2x2000sa-27skle02-v1",
        ]
        params = dict(
            plan_code=plan, fqn=fqn, datacenter="gra", base_item_id=201,
            addons=addons,
            configurations={"dedicated_datacenter": "gra", "region": "europe",
                            "dedicated_os": "none_64.en"},
            cart_items=[
                {"planCode": code, "itemId": 201 + idx,
                 "duration": "P1M", "pricingMode": "default", "quantity": 1}
                for idx, code in enumerate((plan, *addons))
            ],
            cart_configurations=[
                {"label": "dedicated_datacenter", "value": "gra"},
                {"label": "region", "value": "europe"},
                {"label": "dedicated_os", "value": "none_64.en"},
            ],
        )
        valid = identity.inspect_manual_cart_identity(**params)
        self.assertEqual(valid.status, "VERIFIED")
        params["cart_items"][2]["duration"] = "P12M"
        self.assertEqual(identity.inspect_manual_cart_identity(**params).status, "CONFLICT")
        params["cart_items"][2]["duration"] = "P1M"
        params["cart_configurations"][0]["value"] = "bhs"
        self.assertEqual(identity.inspect_manual_cart_identity(**params).status, "CONFLICT")
        params["cart_configurations"][0]["value"] = "gra"
        params["cart_items"] = [{"planCode": plan}]
        self.assertEqual(identity.inspect_manual_cart_identity(**params).status, "UNVERIFIED")
        params["fqn"] = "27skle06-v1.ram-128g-ddr4-ecc-2400.softraid-2x1920nvme"
        self.assertEqual(identity.inspect_manual_cart_identity(**params).status, "CONFLICT")

    def test_manual_ks_4_readback_allows_specific_model_only(self):
        plan = "24sk40-v1"
        ram = "ram-32g-ecc-2133"
        storage = "softraid-2x450nvme"
        addons = [f"{ram}-{plan}", f"{storage}-{plan}"]
        opts = {
            "plan_code": plan,
            "fqn": f"{plan}.{ram}.{storage}",
            "datacenter": "gra",
            "base_item_id": 100,
            "addons": addons,
            "configurations": {"dedicated_datacenter": "gra", "region": "europe",
                               "dedicated_os": "none_64.en"},
            "cart_items": [
                {"planCode": code, "itemId": 100 + idx, "duration": "P1M",
                 "pricingMode": "default", "quantity": 1}
                for idx, code in enumerate((plan, *addons))
            ],
            "cart_configurations": [
                {"label": "dedicated_datacenter", "value": "gra"},
                {"label": "region", "value": "europe"},
                {"label": "dedicated_os", "value": "none_64.en"},
            ],
        }
        self.assertEqual(identity.inspect_manual_cart_identity(**opts).status, "VERIFIED")
        opts["cart_items"][0]["planCode"] = "27skle04-v1"
        self.assertEqual(identity.inspect_manual_cart_identity(**opts).status, "CONFLICT")


if __name__ == "__main__":
    unittest.main()
