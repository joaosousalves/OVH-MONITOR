"""Manual one-cart diagnostic on a currently available alternate Kimsufi.

Never submits checkout/payment, changes live approval, retains a cart, or
writes purchase quota. Contract evidence is private and separate from live.
"""
from __future__ import annotations

import os
import json
import re
from pathlib import Path
import sys

from cart_schema_diagnostic import SchemaTrace
from ksle_monitor import retrieve_stock, ALLOWED_DATACENTERS, MonitorError
from live_purchase import contract_digest, _write_review
from manual_cart_audit import manual_cart_report
from order_probe import (TEST_PLANS, test_order, verify_available_test_fqn,
                         catalog_selection, OrderProbeError)
from purchase_guard import make_api_client, PurchaseSafetyError

DEFAULT_REVIEW = Path("/var/lib/ovh-ksle-purchase/test-contract-review.json")
TEST_MODELS = ("ks-4", "ks-le-1", "ks-le-2", "ks-le-3")


def commercial_values(stage, payload):
    """Bounded allowlist of commercial values; exclude cart/account IDs,
    contract content, settings, free-form descriptions and credentials."""
    def code(value):
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:+-]{1,160}", value):
            return value
        return None

    def money(value):
        if not isinstance(value, dict) or value.get("currencyCode") != "EUR":
            return None
        amount = value.get("value")
        if type(amount) not in (int, float) or not 0 <= amount <= 100000:
            return None
        return {"currencyCode": "EUR", "value": amount}

    def item(row):
        if not isinstance(row, dict):
            return {}
        return {
            **{k: code(row.get(k)) for k in ("productId", "offerId", "duration", "planCode", "pricingMode")},
            "parentItemIdPresent": type(row.get("parentItemId")) is int,
            "optionsCount": len(row["options"]) if isinstance(row.get("options"), list) else None,
            "prices": [{"label": code(p.get("label")), "price": money(p.get("price")),
                        "priceWithTax": money(p.get("priceWithTax"))}
                       for p in (row.get("prices") if isinstance(row.get("prices"), list) else [])[:20]
                       if isinstance(p, dict)],
        }

    if stage in ("base_item", "addon_item"):
        return item(payload)
    if stage == "items_expanded" and isinstance(payload, list):
        return [item(row) for row in payload[:24]]
    if stage == "checkout" and isinstance(payload, dict):
        return [{"detailType": code(row.get("detailType")), "quantity": row.get("quantity")
                 if type(row.get("quantity")) is int else None,
                 "planCode": code(row.get("planCode")), "offerId": code(row.get("offerId")),
                 "unitPrice": money(row.get("unitPrice")), "totalPrice": money(row.get("totalPrice"))}
                for row in (payload.get("details") if isinstance(payload.get("details"), list) else [])[:24]
                if isinstance(row, dict)]
    return None


def diagnose(client, *, review_path=DEFAULT_REVIEW, stock_fetch=retrieve_stock):
    # Match exact mandatory addons against the PT catalogue before any cart.
    catalog = client.request("GET", "/order/catalog/public/eco",
                             query={"ovhSubsidiary": "PT"})
    selected = None
    for model in TEST_MODELS:
        plan = TEST_PLANS[model]
        stocks = stock_fetch(plan_code=plan)
        for stock in stocks:
            if not stock.available or stock.datacenter not in ALLOWED_DATACENTERS:
                continue
            try:
                catalog_selection(catalog, "default", stock.datacenter, plan,
                                  target_fqn=stock.fqn)
            except OrderProbeError:
                continue
            selected = (plan, stock)
            break
        if selected is not None:
            break
    if selected is None:
        raise OrderProbeError("No eligible alternate model currently in stock; no cart created")
    plan, stock = selected
    verify_available_test_fqn(plan, stock.fqn, stock.datacenter)
    observed = {}
    trace = SchemaTrace()
    commercial = []
    def observe_schema(stage, payload=None):
        trace.capture(stage, payload)
        if len(commercial) < 24:
            values = commercial_values(stage, payload)
            if values is not None:
                commercial.append({"stage": stage, "values": values})
    test_order(client, datacenter=stock.datacenter, storage_option="default",
               plan_code=plan, target_fqn=stock.fqn,
               quote_validator=lambda q: observed.__setitem__("quote", q),
               renewal_observer=lambda i: observed.__setitem__("items", i),
               identity_observer=lambda i: observed.__setitem__("identity", i),
               schema_observer=observe_schema)
    quote = observed["quote"]
    items = observed["items"]
    digest, payload = contract_digest(quote.get("contracts"))
    review_path = Path(review_path)
    review_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if review_path.parent.is_symlink() or review_path.parent.stat().st_mode & 0o077:
        raise PurchaseSafetyError("Contract review directory must be private")
    _write_review(review_path, digest, payload)
    print(f"ALTERNATE MODEL TEST: {stock.fqn} at {stock.datacenter.upper()}")
    print(manual_cart_report(quote, items, observed.get("identity")))
    print(trace.report())
    print("COMMERCIAL VALUES (no cart IDs, account data or contracts):")
    print(json.dumps(commercial, ensure_ascii=False, indent=2))
    print(f"CONTRACT SET SHA256: {digest}")
    print(f"Contract evidence saved locally: {review_path}")
    print("NO PURCHASE / NO PAYMENT. Do not approve this fingerprint for KS-LE-6 "
          "until the contracts have been reviewed and the live set matches exactly.")


def main():
    try:
        # Diagnostic uses only the existing non-purchasing cart transport.
        # Do not enable the separate private live checkout transport here.
        os.environ["OVH_LIVE_CHECKOUT_ENABLED"] = "false"
        diagnose(make_api_client())
        return 0
    except (OrderProbeError, PurchaseSafetyError, MonitorError, OSError, ValueError) as exc:
        print(f"Diagnostic incomplete: {type(exc).__name__}; no order/payment submitted.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
