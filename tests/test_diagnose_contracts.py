import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import json

import diagnose_contracts as d
from ksle_monitor import Stock
from order_probe import OrderProbeError
from live_purchase import contract_digest


class DiagnoseContractsTests(unittest.TestCase):
    def test_one_alternate_cart_and_private_review(self):
        plan = d.TEST_PLANS["ks-4"]
        stock = Stock(plan + ".ram-32g-ddr4-ecc-2133.softraid-2x450nvme",
                      "softraid-2x450nvme", "bhs", "1H-low")
        contracts = [{"name": "Terms", "content": "Test terms"}]
        client = Mock()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test-contract-review.json"
            def fake_order(client, **kw):
                self.assertEqual(kw["plan_code"], plan)
                self.assertEqual(kw["target_fqn"], stock.fqn)
                self.assertNotIn("cart_retainer", kw)
                kw["quote_validator"]({"contracts": contracts})
                kw["renewal_observer"]([])
            with patch.object(d, "catalog_selection", return_value=({}, [])), \
                 patch.object(d, "verify_available_test_fqn") as verify, \
                 patch.object(d, "test_order", side_effect=fake_order) as probe, \
                 patch.object(d, "manual_cart_report", return_value="NO PURCHASE"), \
                 patch("builtins.print"):
                d.diagnose(client, review_path=path, stock_fetch=lambda **kw: [stock])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["sha256"], contract_digest(contracts)[0])
            self.assertFalse((Path(td) / "contract-review.json").exists())
            verify.assert_called_once_with(plan, stock.fqn, "bhs")
            probe.assert_called_once()
            client._submit_live_checkout.assert_not_called()

    def test_ks7_catalog_variant_selected_for_diagnostic_only(self):
        from purchase_guard import APPROVED_FQNS
        plan = d.TEST_PLANS["ks-7-alt"]
        stock = Stock(plan + ".ram-128g-ddr4-ecc-2400.softraid-2x4000sa",
                      "softraid-2x4000sa", "bhs", "1H-low")
        client = Mock()
        client.request.return_value = {"plans": [{"planCode": plan}]}
        def fake_order(client, **kw):
            self.assertEqual(kw["plan_code"], plan)
            self.assertNotIn("cart_retainer", kw)
            kw["quote_validator"]({"contracts": [{"name": "Terms", "content": "Test"}]})
            kw["renewal_observer"]([])
        with tempfile.TemporaryDirectory() as td, \
             patch.object(d, "catalog_selection", return_value=({}, [])), \
             patch.object(d, "verify_available_test_fqn"), \
             patch.object(d, "test_order", side_effect=fake_order) as probe, \
             patch.object(d, "manual_cart_report", return_value="NO PURCHASE"), \
             patch("builtins.print"):
            d.diagnose(client, review_path=Path(td) / "test.json",
                       stock_fetch=lambda **kw: [stock] if kw["plan_code"] == plan else [])
            probe.assert_called_once()
        self.assertNotIn(stock.fqn, APPROVED_FQNS)
        client._submit_live_checkout.assert_not_called()

    def test_no_stock_no_cart(self):
        with patch.object(d, "test_order") as probe:
            with self.assertRaises(OrderProbeError):
                d.diagnose(Mock(), stock_fetch=lambda **kw: [])
        probe.assert_not_called()
