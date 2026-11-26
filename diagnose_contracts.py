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
TEST_MODELS = ("ks-7", "ks-7-alt", "ks-7-legacy", "ks-4", "ks-le-1", "ks-le-2", "ks-le-3")


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
    print("Public PT catalog retrieved.", flush=True)
    for model in TEST_MODELS:
        plan = TEST_PLANS[model]
        if model.startswith("ks-7"):
            # The precise plan code depends on the offer version. Only use
            # a known KS-7 code actually present in the Portuguese catalog.
            plans = catalog.get("plans", []) if isinstance(catalog, dict) else []
            if not any(isinstance(p, dict) and p.get("planCode") == plan for p in plans):
                print(f"{model}: {plan} not in PT catalog; skipped.", flush=True)
                continue
        try:
            stocks = stock_fetch(plan_code=plan)
        except MonitorError as exc:
            if model.startswith("ks-7") and str(exc) == f"No {plan} stock records in OVHcloud API response":
                print(f"{model}: no exact stock records for {plan}; skipped.", flush=True)
                continue
            raise
        available = [s for s in stocks if s.available and s.datacenter in ALLOWED_DATACENTERS]
        print(f"{model}: {len(available)} available EU/CA configuration(s)", flush=True)
        for stock in stocks:
            if not stock.available or stock.datacenter not in ALLOWED_DATACENTERS:
                continue
            try:
                catalog_selection(catalog, "default", stock.datacenter, plan,
                                  target_fqn=stock.fqn)
            except OrderProbeError as exc:
                # catalog_selection uses only the public catalog and the
                # public stock FQN; no authenticated cart exists here.
                safe_fqn = stock.fqn if re.fullmatch(r"[a-zA-Z0-9_.-]{1,200}", stock.fqn) else "<invalid FQN>"
                reason = str(exc)
                if not re.fullmatch(r"[a-zA-Z0-9 ._='/-]{1,220}", reason):
                    reason = "Catalog selection rejected (unexpected error format)"
                print(f"Rejected {safe_fqn} at {stock.datacenter.upper()}: {reason}", flush=True)
                plan_row = next((p for p in catalog.get("plans", [])
                                 if isinstance(p, dict) and p.get("planCode") == plan), {})
                for family in plan_row.get("addonFamilies", []):
                    if not isinstance(family, dict):
                        continue
                    name = family.get("name")
                    if name not in ("storage", "memory"):
                        continue
                    addons = family.get("addons")
                    codes = [v for v in addons[:40]
                             if isinstance(v, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,160}", v)] if isinstance(addons, list) else []
                    print(f"  Catalog {name} (mandatory={family.get('mandatory') is True}): {codes}", flush=True)
                continue
            selected = (plan, stock)
            break
        if selected is not None:
            break
    if selected is None:
        raise OrderProbeError("No eligible alternate model currently in stock; no cart created")
    plan, stock = selected
    print(f"Selected {plan}, {stock.datacenter.upper()}; rechecking exact stock.", flush=True)
    verify_available_test_fqn(plan, stock.fqn, stock.datacenter)
    print("Exact stock confirmed; preparing one temporary cart.", flush=True)
    observed = {}
    trace = SchemaTrace()
    commercial = []
    def observe_schema(stage, payload=None):
        trace.capture(stage, payload)
        print(f"Diagnostic stage: {stage}", flush=True)
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


def safe_error(exc):
    """Only fixed descriptions and HTTP status; never echo raw API errors."""
    message = str(exc)
    if message == "No eligible alternate model currently in stock; no cart created":
        return "No currently available alternate model matches the PT catalog"
    if message.startswith("Requested configuration now has status"):
        return "Stock disappeared during exact configuration recheck"
    if message.startswith("Requested hardware FQN/datacenter not present"):
        return "Exact configuration disappeared from the stock feed"
    if message.startswith("No P1M/default offer found"):
        return "Selected cart has no monthly default offer"
    status = re.search(r"OVHcloud API ([1-5][0-9]{2}) on (GET|POST|DELETE) ", message)
    if status:
        route = message.split(" on ", 1)[1].split(":", 1)[0]
        # Strip all dynamic cart and item identifiers from the endpoint.
        route = re.sub(r"(/order/cart/)[^/ ]+", r"\g<1>{cartId}", route)
        route = re.sub(r"(/item/)[^/ ]+", r"\g<1>{itemId}", route)
        if not re.fullmatch(r"(GET|POST|DELETE) /order/(?:catalog/public/eco|cart(?:/\{cartId\}(?:/(?:assign|eco(?:/options)?|checkout|item(?:/\{itemId\}(?:/configuration(?:/[0-9]+)?)?)?))?)?)", route):
            route = status.group(2) + " request"
        return f"OVH HTTP {status.group(1)} at {route}"
    if message.startswith("OVHcloud connection error"):
        return "OVH API connection or timeout error"
    if message.startswith("Unable to verify current"):
        return "Stock API recheck failed"
    return type(exc).__name__ + " (see last diagnostic stage above)"


def main():
    try:
        # Diagnostic uses only the existing non-purchasing cart transport.
        # Do not enable the separate private live checkout transport here.
        os.environ["OVH_LIVE_CHECKOUT_ENABLED"] = "false"
        diagnose(make_api_client())
        return 0
    except (OrderProbeError, PurchaseSafetyError, MonitorError, OSError, ValueError) as exc:
        print(f"Diagnostic incomplete: {safe_error(exc)}; no order/payment submitted.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
