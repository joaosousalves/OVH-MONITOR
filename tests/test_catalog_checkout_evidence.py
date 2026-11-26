import os
import tempfile
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

import live_purchase as live
from catalog_checkout_evidence import catalog_quote_evidence
from cart_receipts import verify_creation_receipts
from purchase_guard import PurchasePolicy, PurchaseSafetyError, APPROVED_FQNS

def money(amount):
    return {"currencyCode": "EUR", "value": amount}

def fixture():
    fqn = APPROVED_FQNS[0]
    plan, ram, disk = fqn.split(".")
    codes = [plan, "bandwidth-500-ks-gen0", ram + "-" + plan, disk + "-" + plan]
    configs = {"dedicated_datacenter":"bhs", "region":"canada", "dedicated_os":"none_64.en"}
    def component(code, setup, month):
        return {"planCode":code,"pricings":[
            {"phase":0,"interval":0,"intervalUnit":"none","mode":"default",
             "commitment":0,"type":"rental","price":setup,"tax":setup*23//100},
            {"phase":1,"interval":1,"intervalUnit":"month","mode":"default",
             "commitment":0,"type":"rental","price":month,"tax":month*23//100},
            {"phase":1,"interval":12,"intervalUnit":"month","mode":"upfront12",
             "commitment":12,"type":"rental","price":month*12,"tax":month*12*23//100}]}
    base = component(plan,2699000000,2699000000)
    base["configurations"] = [{"name":k,"values":[v]} for k,v in configs.items()]
    base["addonFamilies"] = [
        {"name":name,"mandatory":True,"addons":[code],"default":code}
        for name,code in zip(("bandwidth","memory","storage"),codes[1:])]
    catalog = {"locale":{"currencyCode":"EUR","taxRate":23,"subsidiary":"PT"},
               "plans":[base],"addons":[component(code,0,0) for code in codes[1:]]}
    catalog["addons"][0]["pricings"].append(deepcopy(catalog["addons"][0]["pricings"][0]))
    def detail(kind,amount):
        return {"detailType":kind,"quantity":1,"unitPrice":money(amount),"totalPrice":money(amount)}
    quote = {"prices":{"withoutTax":money(53.98),"tax":money(12.42),
                       "withTax":money(66.40)},"details":[detail("DURATION",v) for v in (26.99,0,0,0)]
              + [detail("INSTALLATION",26.99),detail("INSTALLATION",0)],
              "contracts":[{"name":"Example terms","content":"Fixture only, no actual legal approval"}]}
    items = [{"itemId":i,"productId":"eco","duration":"P1M","options":[2,3,4] if i==1 else [],
              "prices":[{"label":"TOTAL","price":money(26.99 if i==1 else 0)}]} for i in range(1,5)]
    record = {"cart_id":"cart-example","item_id":1,"fqn":fqn,"datacenter":"bhs",
              "receipts":[{"code":c,"id":i} for i,c in enumerate(codes,1)]}
    config_rows = [{"label":k,"value":v} for k,v in configs.items()]
    return catalog, quote, record, items, configs, config_rows, codes

class CatalogCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.catalog,self.quote,self.record,self.items,self.configs,self.config_rows,self.codes=fixture()
    def check_quote(self):
        return catalog_quote_evidence(self.catalog,self.record["fqn"],"bhs",self.quote,
            max_initial=Decimal("66.40"),max_monthly=Decimal("33.20"))
    def test_observed_schema(self):
        initial,monthly=self.check_quote()
        self.assertEqual(initial.with_tax,Decimal("66.40"))
        self.assertEqual(monthly,Decimal("33.20"))
    def test_wrong_tax(self):
        self.catalog["plans"][0]["pricings"][1]["tax"]+=1
        with self.assertRaises(PurchaseSafetyError): self.check_quote()
    def test_wrong_details(self):
        self.quote["details"][0]["quantity"]=2
        with self.assertRaises(PurchaseSafetyError): self.check_quote()
    def test_receipts_graph(self):
        self.assertTrue(verify_creation_receipts(self.record,self.codes[1:],self.items,self.config_rows,self.configs))
        self.items[0]["options"]=[2,3,5]
        with self.assertRaises(PurchaseSafetyError):
            verify_creation_receipts(self.record,self.codes[1:],self.items,self.config_rows,self.configs)
    def test_live_gate_with_realistic_missing_planCode_and_RENEW(self):
        client=Mock()
        def request(method,path,**kwargs):
            self.assertEqual(method,"GET")
            if path.endswith("/eco"): return self.catalog
            if path.endswith("/configuration"): return self.config_rows
            if path.endswith("/item"): return self.items
            if path.endswith("/checkout"): return self.quote
            raise AssertionError(path)
        client.request.side_effect=request
        policy=PurchasePolicy.from_env({"OVH_AUTO_BUY_MAX_TOTAL_EUR":"90.00","OVH_AUTO_BUY_MAX_MONTHLY_EUR":"37.00"})
        digest,_=live.contract_digest(self.quote["contracts"])
        with tempfile.TemporaryDirectory() as td, patch.object(live,"inspect_payment_evidence",return_value=Mock(saved_default_valid=True)):
            review=Path(td)/"review.json"
            with patch.dict(os.environ,{"OVH_LIVE_APPROVED_CONTRACT_SHA256":""}):
                with self.assertRaisesRegex(PurchaseSafetyError,"fingerprint"):
                    live.verify_retained(client,policy,self.record,review)
            with patch.dict(os.environ,{"OVH_LIVE_APPROVED_CONTRACT_SHA256":digest}):
                self.assertEqual(live.verify_retained(client,policy,self.record,review),Decimal("66.40"))
        client._submit_live_checkout.assert_not_called()
