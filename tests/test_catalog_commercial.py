import unittest
from catalog_commercial import audit_offer
from ksle_monitor import PLAN_CODE, REQUIRED_MEMORY, STORAGE_PRIORITIES

class CatalogCommercialTests(unittest.TestCase):
    def setUp(self):
        disk = STORAGE_PRIORITIES[0]
        self.fqn = f"{PLAN_CODE}.{REQUIRED_MEMORY}.{disk}"
        def row(code, amount):
            return {"planCode": code, "pricings": [
                {"phase": 1, "interval": 1, "intervalUnit": "month",
                 "mode": "default", "commitment": 0, "price": amount}]}
        ram = f"{REQUIRED_MEMORY}-{PLAN_CODE}"
        storage = f"{disk}-{PLAN_CODE}"
        base = row(PLAN_CODE, 2699000000)
        base["configurations"] = [{"name": k, "values": [v]} for k, v in
             (("dedicated_datacenter", "bhs"), ("region", "canada"),
              ("dedicated_os", "none_64.en"))]
        base["addonFamilies"] = [
             {"name": "memory", "mandatory": True, "addons": [ram]},
             {"name": "storage", "mandatory": True, "addons": [storage]}]
        self.catalog = {"locale": {"currencyCode": "EUR", "subsidiary": "PT"},
                        "plans": [base], "addons": [row(ram, 0), row(storage, 0)]}
    def test_monthly_exact(self):
        result = audit_offer(self.catalog, self.fqn, "bhs")
        self.assertEqual(result["monthlyGrossEUR"], "33.20")
        self.assertTrue(result["notAboveReference"])
    def test_missing_commitment_blocks_catalog_proof(self):
        del self.catalog["addons"][0]["pricings"][0]["commitment"]
        self.assertNotIn("monthlyGrossEUR", audit_offer(self.catalog, self.fqn, "bhs"))
    def test_positive_commitment_blocks_catalog_proof(self):
        self.catalog["plans"][0]["pricings"][0]["commitment"] = 12
        self.assertNotIn("monthlyGrossEUR", audit_offer(self.catalog, self.fqn, "bhs"))
    def test_different_mode_blocks_catalog_proof(self):
        self.catalog["plans"][0]["pricings"][0]["mode"] = "commitment"
        self.assertNotIn("monthlyGrossEUR", audit_offer(self.catalog, self.fqn, "bhs"))
