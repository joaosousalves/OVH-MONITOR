"""Privacy and bounded-output tests for the manual cart shape-only report."""

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cart_schema_diagnostic import SchemaTrace


class SchemaTraceTests(unittest.TestCase):
    def test_never_displays_untrusted_keys_values_identifiers_or_urls(self):
        forbidden = (
            "TOP_SECRET_PAYMENT_123", "unknownPrivateField", "private-url-987",
            "customer@example.com", "untrusted-RENEW-label", "cart-12345",
        )
        trace = SchemaTrace()
        trace.capture("base_item", {
            "itemId": 123456, "planCode": "private-url-987",
            "unknownPrivateField": "TOP_SECRET_PAYMENT_123",
            "prices": [
                {"label": "untrusted-RENEW-label", "description": "customer@example.com"},
                {"label": "RENEW", "price": {
                    "value": "TOP_SECRET_PAYMENT_123", "currencyCode": "private-url-987",
                }, "priceWithTax": {"text": "cart-12345"}},
            ],
        })
        trace.capture("checkout", {
            "prices": {"withTax": {"value": "TOP_SECRET_PAYMENT_123",
                                   "text": "private-url-987"}},
            "details": [{"detailType": "unknownPrivateField",
                         "description": "customer@example.com"}],
            "contracts": [{"url": "private-url-987", "id": "cart-12345"}],
        })
        trace.capture("items_get", [123456, 987654])
        trace.capture("items_expanded", [{"planCode": "customer@example.com",
                                         "someWeirdKey": "TOP_SECRET_PAYMENT_123"}])
        trace.capture("configurations_expanded", [{
            "label": "unknownPrivateField", "value": "private-url-987",
        }])
        report = trace.report()
        for secret in forbidden:
            self.assertNotIn(secret, report)
        self.assertIn("RENEW=1", report)
        self.assertIn("OTHER=1", report)
        self.assertIn("planCode:string", report)
        self.assertIn("quantity:missing", report)
        self.assertIn("prices.withTax=[", report)

    def test_reports_missing_renew_and_incomplete_identity_evidence(self):
        trace = SchemaTrace()
        trace.capture("addon_item", {"itemId": 42})
        trace.capture("items_get", [{"itemId": 42}])
        trace.capture("items_expanded", [{"itemId": 42}])
        trace.capture("configurations_get", [{"label": "region"}])
        trace.capture("configurations_expanded", [{"label": "region"}])
        report = trace.report()
        self.assertIn("prices=missing", report)
        self.assertIn("planCode:missing", report)
        self.assertIn("value:missing", report)
        self.assertIn("region=1", report)
        self.assertNotIn("42", report)

    def test_optional_get_denied_is_a_fixed_message(self):
        trace = SchemaTrace()
        trace.capture("readback_unavailable", Exception("CUSTOMER-PRIVATE-INFO"))
        self.assertIn("read-only identity evidence unavailable", trace.report())
        self.assertNotIn("CUSTOMER-PRIVATE-INFO", trace.report())

    def test_readback_detects_renew_labels_without_disclosing_price_or_item(self):
        trace = SchemaTrace()
        trace.capture("items_expanded", [{
            "planCode": "VERY-PRIVATE-CODE", "itemId": 734528,
            "prices": [{"label": "RENEW", "price": {"value": "TOP_SECRET_PRICE"}}],
        }])
        report = trace.report()
        self.assertIn("item prices=[list=1]", report)
        self.assertIn("first item labels=1 row(s) [RENEW=1]", report)
        self.assertNotIn("TOP_SECRET_PRICE", report)
        self.assertNotIn("734528", report)
        self.assertNotIn("VERY-PRIVATE-CODE", report)

    def test_bounded_huge_and_malicious_responses(self):
        trace = SchemaTrace()
        for index in range(250):
            trace.capture("addon_item", {
                "prices": [{"label": "RENEW", "price": {"value": "private"}}] * 500,
                "privateField": "TOP_SECRET_PAYMENT_123",
            })
        trace.capture("arbitrary-stage-TOP_SECRET_PAYMENT_123", "secret")
        report = trace.report()
        self.assertLess(len(report), 13000)
        self.assertIn("Additional diagnostic stages omitted", report)
        self.assertNotIn("TOP_SECRET_PAYMENT_123", report)


if __name__ == "__main__":
    unittest.main()
