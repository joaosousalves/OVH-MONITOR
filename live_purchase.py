"""Opt-in checkout of one exact retained KS-LE-6 cart.

The ordinary diagnostic API still prohibits checkout. A saved payment method
is readiness only; an order ID is never reported as successful payment.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from decimal import Decimal
from pathlib import Path

from cart_identity import inspect_cart_identity
from catalog_checkout_evidence import catalog_quote_evidence
from commercial_audit import validate_initial_price, _renewal_vat_gross
from commitment_guard import inspect_commitment, CommitmentSafetyError
from confirmed_terms import for_storage
from order_probe import (
    OrderProbeError, catalog_selection, expand_cart_readback,
    PREFERRED_STORAGE_OPTION, test_order,
)
from payment_evidence import inspect_payment_evidence
from pending_orders import reconcile_pending_orders
from purchase_guard import PurchaseSafetyError, choose_candidate
from purchase_quota import OPEN_STATES
from retained_cart import read_retained, ensure_no_active_cart, persist_retained, RetainedCartError
from renewal_pricing import inspect_cart_renewals


def enabled():
    return os.getenv("OVH_LIVE_CHECKOUT_ENABLED", "").strip().lower() == "true"


def contract_digest(contracts):
    if (not isinstance(contracts, list) or not 0 < len(contracts) <= 20
            or any(not isinstance(c, dict) or not isinstance(c.get("name"), str)
                   or not (c.get("content") or c.get("url")) for c in contracts)):
        raise PurchaseSafetyError("Complete checkout contracts are unavailable")
    payload = json.dumps(contracts, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    if len(payload) > 1_000_000:
        raise PurchaseSafetyError("Contract evidence exceeds review limit")
    return hashlib.sha256(payload).hexdigest(), payload


def _write_review(path, digest, payload):
    # Local, private operator evidence, not email or journal output.
    temp = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent,
                                         encoding="utf-8", delete=False) as f:
            temp = Path(f.name)
            os.fchmod(f.fileno(), 0o600)
            json.dump({"sha256": digest, "contracts": json.loads(payload)}, f,
                      ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _reconcile_local(client, quota, retained_path):
    quota._check_legacy()
    entries = quota.read()
    if any(row["status"] in ("reserved", "unknown") for row in entries):
        raise PurchaseSafetyError("Uncertain earlier checkout: reconcile manually; never retry")
    for row in entries:
        if row["status"] != "submitted":
            continue
        response = client.request("GET", f"/me/order/{row['orderId']}/status")
        status = response.get("status") if isinstance(response, dict) else response
        if status != "delivered":
            raise PurchaseSafetyError("Earlier submitted order not yet delivered; no duplicate checkout")
        inventory = reconcile_pending_orders(client, quota.max_servers)
        if inventory.overlap_unverified or inventory.services.matching_services < len(entries):
            raise PurchaseSafetyError("Delivered order not yet reconciled with account services")
        quota.update_outcome(slot=row["slot"], status="confirmed", order_id=row["orderId"])
        # The only retained cart belongs to the previously submitted checkout.
        # Do not DELETE it; OVH owns the submitted order lifecycle.
        record = read_retained(retained_path)
        if record is not None and row.get("cartId") == record["cart_id"]:
            retained_path.unlink(missing_ok=True)
    return quota.read()


def verify_retained(client, policy, record, review_path):
    """Re-read current cart; never rely on a prior email or stale quote."""
    if policy.max_total_eur > Decimal("90.00") or policy.max_monthly_eur is None or policy.max_monthly_eur > Decimal("37.00"):
        raise PurchaseSafetyError("Live caps must be <= 90 initial / 37 monthly EUR")
    fqn, dc = record["fqn"], record["datacenter"]
    if dc not in policy.datacenters:
        raise PurchaseSafetyError("Retained datacenter outside approved policy")
    reference = for_storage(fqn.split(".")[2])
    base = "/order/cart/" + record["cart_id"]
    catalog = client.request("GET", "/order/catalog/public/eco",
                             query={"ovhSubsidiary": "PT"})
    configs, addons = catalog_selection(catalog, PREFERRED_STORAGE_OPTION, dc,
                                        target_fqn=fqn)
    ipath = base + "/item"
    items = expand_cart_readback(client, ipath, client.request("GET", ipath), "itemId")
    cpath = ipath + f"/{record['item_id']}/configuration"
    configuration = expand_cart_readback(client, cpath, client.request("GET", cpath),
                                          "configurationId")
    identity = inspect_cart_identity(
        fqn=fqn, datacenter=dc, base_item_id=record["item_id"], addons=addons,
        configurations=configs, cart_items=items, cart_configurations=configuration,
    )
    quote = client.request("GET", base + "/checkout")
    initial = validate_initial_price(quote, min(policy.max_total_eur, reference.first_payment_gross))
    # Save the exact contract evidence even if another check is blocked.
    digest, payload = contract_digest(quote.get("contracts"))
    _write_review(review_path, digest, payload)
    if not identity.verified:
        raise PurchaseSafetyError("Exact retained cart identity not verified")
    inspect_commitment(quote, *items)
    monthly_cap = min(policy.max_monthly_eur, reference.monthly_gross)
    initial_cap = min(policy.max_total_eur, reference.first_payment_gross)
    initial, monthly = catalog_quote_evidence(
        catalog, fqn, dc, quote, max_initial=initial_cap, max_monthly=monthly_cap)
    # Explicit renewal rows, if returned, must not contradict the catalog.
    has_renewal = any(isinstance(p, dict) and p.get("label") == "RENEW"
                      for item in items if isinstance(item, dict)
                      for p in item.get("prices", []) if isinstance(item.get("prices"), list))
    if has_renewal:
        explicit, reason = _renewal_vat_gross(items)
        renewal = inspect_cart_renewals(items)
        if explicit != monthly or renewal.amount_eur is None or explicit < renewal.amount_eur:
            raise PurchaseSafetyError("Explicit renewal differs from catalog: " + reason)
    # An explicit owner approval of the exact reviewed contract set is
    # required; unknown legal terms must not be accepted automatically.
    approved = os.getenv("OVH_LIVE_APPROVED_CONTRACT_SHA256", "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", approved) or approved != digest:
        raise PurchaseSafetyError("Review contract terms for no fixed commitment; fingerprint " + digest)
    if not inspect_payment_evidence(client).saved_default_valid:
        raise PurchaseSafetyError("No verified valid default one-click payment method")
    final_quote = client.request("GET", base + "/checkout")
    final_initial, final_monthly = catalog_quote_evidence(
        catalog, fqn, dc, final_quote, max_initial=initial_cap, max_monthly=monthly_cap)
    final_digest, _ = contract_digest(final_quote.get("contracts"))
    inspect_commitment(final_quote)
    if final_initial != initial or final_monthly != monthly or final_digest != digest:
        raise PurchaseSafetyError("Quote or contracts changed during checkout verification")
    return final_initial.with_tax


def prepare_live_cart(policy, client, stocks, journal, quota, retained_path):
    """Build immediately after a fresh stock poll; all billable checks follow.

    No remote inventory scan is needed before a non-billable cart. The
    independent account scan is mandatory before checkout.
    """
    if not enabled():
        raise PurchaseSafetyError("Live checkout disabled")
    if journal.read() is not None:
        raise PurchaseSafetyError("Unresolved legacy purchase journal")
    quota._check_legacy()
    entries = quota.read()
    if len(entries) >= policy.max_servers or any(r["status"] in OPEN_STATES for r in entries):
        raise PurchaseSafetyError("Local quota unavailable")
    if policy.max_total_eur > Decimal("90.00") or policy.max_monthly_eur is None or policy.max_monthly_eur > Decimal("37.00"):
        raise PurchaseSafetyError("Live caps must be <= 90 initial / 37 monthly EUR")
    candidate = choose_candidate(stocks, policy)
    if candidate is None:
        raise PurchaseSafetyError("No eligible stock")
    ensure_no_active_cart(retained_path, client)
    reference = for_storage(candidate.storage)
    evidence = {}

    def initial(quote):
        evidence["initial"] = validate_initial_price(
            quote, min(policy.max_total_eur, reference.first_payment_gross))
        inspect_commitment(quote)

    def renewals(items):
        inspect_commitment(*items)
        monthly, _ = _renewal_vat_gross(items)
        if monthly is not None and monthly > min(policy.max_monthly_eur, reference.monthly_gross):
            raise PurchaseSafetyError("Cart renewal exceeds approved reference/cap")

    def identity(value):
        if value.status == "CONFLICT":
            raise PurchaseSafetyError("Cart identity conflicts with exact configuration")
        evidence["identity"] = value.status

    def retain(**record):
        if "initial" not in evidence or "identity" not in evidence:
            return False
        return persist_retained(retained_path, **record)

    from cart_schema_diagnostic import SchemaTrace
    trace = SchemaTrace()
    test_order(client, datacenter=candidate.datacenter, target_fqn=candidate.fqn,
               quote_validator=initial, renewal_observer=renewals,
               identity_observer=identity, schema_observer=trace.capture,
               cart_retainer=retain)
    if read_retained(retained_path) is None:
        raise PurchaseSafetyError("Cart could not be retained")
    return (f"LIVE CART PREPARED: {candidate.fqn} at {candidate.datacenter.upper()}; "
            f"initial {evidence['initial'].with_tax:.2f} EUR incl. VAT; "
            f"identity {evidence['identity']}; no order yet.\n" + trace.report()[:4500])


def try_retained_checkout(policy, client, journal, quota, retained_path):
    """Return None if a new cart may be built; otherwise an honest status.

    Single-host lock plus quota reservation is durable BEFORE the only POST.
    Any exception or malformed response after reservation is uncertain.
    """
    if not enabled():
        return None
    retained_path = Path(retained_path)
    lock_path = retained_path.with_name("live-checkout.lock")
    retained_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if retained_path.parent.stat().st_mode & 0o077:
        raise PurchaseSafetyError("Live checkout directory not private")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_mode & 0o077:
            raise PurchaseSafetyError("Unsafe live checkout lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        if journal.read() is not None:
            raise PurchaseSafetyError("Unresolved legacy purchase journal")
        entries = _reconcile_local(client, quota, retained_path)
        if len(entries) >= policy.max_servers:
            return "LIVE CHECKOUT: local purchase quota reached"
        record = read_retained(retained_path)
        if record is None:
            return None
        if any(row.get("cartId") == record["cart_id"] for row in entries):
            # A crash between confirming a delivery and unlinking the
            # retained state must never allow the same cart to be reused.
            retained_path.unlink()
            return None
        try:
            ensure_no_active_cart(retained_path, client)
        except RetainedCartError as exc:
            # Only an explicitly active cart may proceed to fresh readback.
            if str(exc) != "Existing retained cart requires review; no duplicate cart":
                raise
        else:
            return None  # expired or missing; no order
        # Fresh quota scan, independent of the earlier commercial audit.
        remote = reconcile_pending_orders(client, policy.max_servers)
        if (remote.overlap_unverified or remote.upper_bound >= policy.max_servers
                or remote.upper_bound < len(entries)):
            raise PurchaseSafetyError("Remote quota full or not reconciled")
        total = verify_retained(client, policy, record,
                                retained_path.with_name("contract-review.json"))
        slot = quota.reserve(fqn=record["fqn"], datacenter=record["datacenter"], total=total,
                             cart_id=record["cart_id"])
        if slot is None:
            raise PurchaseSafetyError("Local quota reached")
        try:
            response = client._submit_live_checkout(record["cart_id"])
            order_id = response.get("orderId") if isinstance(response, dict) else None
            if type(order_id) is not int or not 0 < order_id < 10**15:
                raise OrderProbeError("Checkout response missing valid order identifier")
        except Exception:
            quota.update_outcome(slot=slot, status="unknown")
            return "LIVE CHECKOUT UNKNOWN: possible order; do not retry; reconcile in OVH Manager"
        quota.update_outcome(slot=slot, status="submitted", order_id=order_id)
        # Do not interpret an order ID as payment confirmation.
        return (f"LIVE CHECKOUT SUBMITTED: order {order_id}; quoted initial "
                f"{total:.2f} EUR incl. VAT; payment requested, NOT confirmed; "
                "further checkout blocked until reconciliation")
    except (PurchaseSafetyError, OrderProbeError, CommitmentSafetyError) as exc:
        # Errors raised by transport can include private IDs; do not echo them.
        message = str(exc) if isinstance(exc, (PurchaseSafetyError, CommitmentSafetyError)) else "Cart or API evidence unavailable"
        return "LIVE CHECKOUT BLOCKED: " + message
    finally:
        os.close(fd)
