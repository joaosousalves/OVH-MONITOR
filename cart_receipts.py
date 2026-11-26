"""Correlate exact POST selection receipts with fresh Eco readback.

OVH Eco readback often omits planCode, quantity and pricingMode. The
documented options array contains option item IDs. Never infer identity from
price or item count alone; require the complete creation receipt and graph.
"""
from commitment_guard import inspect_commitment
from purchase_guard import PurchaseSafetyError


def creation_receipt(code, response):
    if (not isinstance(response, dict) or type(response.get("itemId")) is not int
            or not 0 < response["itemId"] < 10**15
            or response.get("productId") != "eco" or response.get("duration") != "P1M"):
        raise PurchaseSafetyError("Missing exact Eco creation receipt")
    inspect_commitment(response)
    for field, expected in (("planCode", code), ("pricingMode", "default"), ("quantity", 1)):
        if field in response and (response[field] != expected
                                  or (field == "quantity" and type(response[field]) is not int)):
            raise PurchaseSafetyError("Creation receipt conflicts with selected item")
    return {"code": code, "id": response["itemId"]}


def verify_creation_receipts(record, addons, items, configurations, expected_config):
    receipts = record.get("receipts")
    codes = [record["fqn"].split(".")[0], *addons]
    if (not isinstance(receipts, list) or len(receipts) != len(codes)
            or not 1 < len(receipts) <= 24
            or any(not isinstance(r, dict) or set(r) != {"code", "id"}
                   or type(r["id"]) is not int or not 0 < r["id"] < 10**15
                   or not isinstance(r["code"], str) for r in receipts)):
        raise PurchaseSafetyError("Missing durable exact creation receipts")
    by_code = {r["code"]: r["id"] for r in receipts}
    if (len(by_code) != len(codes) or set(by_code) != set(codes)
            or len(set(by_code.values())) != len(codes)
            or by_code[codes[0]] != record["item_id"]):
        raise PurchaseSafetyError("Creation receipt item IDs or catalog selection changed")
    if not isinstance(items, list) or len(items) != len(codes):
        raise PurchaseSafetyError("Unexpected cart item count")
    id_to_code = {v:k for k,v in by_code.items()}
    seen = set()
    base = record["item_id"]
    for item in items:
        if not isinstance(item, dict):
            raise PurchaseSafetyError("Malformed cart item")
        item_id = item.get("itemId")
        if type(item_id) is not int or item_id not in id_to_code or item_id in seen:
            raise PurchaseSafetyError("Unexpected or duplicate readback item ID")
        seen.add(item_id)
        creation_receipt(id_to_code[item_id], item)
        if item.get("cartId", record["cart_id"]) != record["cart_id"]:
            raise PurchaseSafetyError("Readback cart ID mismatch")
        parent = item.get("parentItemId")
        options = item.get("options")
        if not isinstance(options, list) or any(type(v) is not int for v in options):
            raise PurchaseSafetyError("Cart option ID graph unavailable")
        if item_id == base:
            expected = set(id_to_code) - {base}
            if parent not in (None, 0) or len(options) != len(expected) or set(options) != expected:
                raise PurchaseSafetyError("Base cart item option graph mismatch")
        elif options or (parent is not None and parent != base):
            raise PurchaseSafetyError("Unexpected nested item or parent ID")
    if (not isinstance(configurations, list) or len(configurations) != len(expected_config)
            or any(not isinstance(r, dict) or not isinstance(r.get("label"), str)
                   for r in configurations)
            or {r["label"]: r.get("value") for r in configurations} != expected_config):
        raise PurchaseSafetyError("Exact cart location/OS configuration mismatch")
    return True
