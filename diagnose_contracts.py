"""Manual one-cart diagnostic on a currently available alternate Kimsufi.

Never submits checkout/payment, changes live approval, retains a cart, or
writes purchase quota. Contract evidence is private and separate from live.
"""
from __future__ import annotations

import os
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
    test_order(client, datacenter=stock.datacenter, storage_option="default",
               plan_code=plan, target_fqn=stock.fqn,
               quote_validator=lambda q: observed.__setitem__("quote", q),
               renewal_observer=lambda i: observed.__setitem__("items", i),
               identity_observer=lambda i: observed.__setitem__("identity", i),
               schema_observer=trace.capture)
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
