"""Conservative commercial preflight for KS-LE-6; no checkout or payment.

Only run manually when stock is actually reported. Remote outstanding orders
are checked *before* building a temporary cart. An initial cart total is not
a guarantee about monthly renewal price or minimum contract commitment.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import logging
import os
from pathlib import Path
from typing import Any, Callable, Sequence

from account_reconciliation import AccountReconciliationError
from commitment_guard import CommitmentSafetyError, inspect_commitment
from ksle_monitor import APITransientError, MonitorError, Stock, retrieve_stock
from order_probe import OVHCartClient, OrderProbeError, test_order, verify_available_test_fqn
from pending_orders import PendingOrderInventory, PendingOrdersError, reconcile_pending_orders
from purchase_guard import (
    DEFAULT_JOURNAL, OrderJournal, PurchasePolicy, PurchaseSafetyError,
    checkout_total_eur, choose_candidate, make_api_client, parse_eur,
)
from purchase_quota import DEFAULT_PATH, OPEN_STATES, PurchaseQuota
from renewal_pricing import inspect_cart_renewals


LOG = logging.getLogger("ksle-commercial-audit")


def _price_field(prices: Any, name: str, *, zero_ok: bool) -> Decimal:
    if not isinstance(prices, dict):
        raise PurchaseSafetyError("Incomplete initial VAT breakdown")
    field = prices.get(name)
    if not isinstance(field, dict) or field.get("currencyCode") != "EUR":
        raise PurchaseSafetyError("Incomplete initial VAT breakdown in EUR")
    return parse_eur(field.get("value"), label=f"initial {name}", allow_zero=zero_ok)


@dataclass(frozen=True)
class InitialPrice:
    without_tax: Decimal
    vat: Decimal
    with_tax: Decimal


def validate_initial_price(quote: Any, maximum: Decimal) -> InitialPrice:
    """Confirm the complete initial cart gross/net/tax arithmetic in EUR.

    The gross quote covers all cart charges; it does NOT separately prove a
    particular setup fee or guarantee a future monthly renewal amount.
    """
    gross = checkout_total_eur(quote)
    prices = quote.get("prices")
    net = _price_field(prices, "withoutTax", zero_ok=True)
    vat = _price_field(prices, "tax", zero_ok=True)
    if net + vat != gross:
        raise PurchaseSafetyError("Initial VAT breakdown does not reconcile")
    if gross > maximum:
        raise PurchaseSafetyError(
            f"PRICE BLOCKED: {gross:.2f} EUR initial incl. VAT exceeds {maximum:.2f} EUR"
        )
    return InitialPrice(net, vat, gross)


def _renewal_vat_gross(items: Sequence[Any]) -> tuple[Decimal | None, str]:
    """Recognize only explicit, consistent EUR-with-tax RENEW item prices.

    Generic RENEW `price` values are ambiguous about tax and deliberately do
    not qualify. Even an explicit gross field is *evidence*, not a binding
    promise of future monthly charges.
    """
    if not isinstance(items, (list, tuple)) or not items:
        return None, "missing cart item responses"
    total = Decimal("0.00")
    for item in items:
        if not isinstance(item, dict) or item.get("duration", "P1M") != "P1M":
            return None, "non-monthly or malformed cart item"
        prices = item.get("prices")
        if not isinstance(prices, list):
            return None, "missing cart item price labels"
        rows = [row for row in prices if isinstance(row, dict) and row.get("label") == "RENEW"]
        if len(rows) != 1:
            return None, "missing or ambiguous RENEW label"
        gross = rows[0].get("priceWithTax")
        if not isinstance(gross, dict) or gross.get("currencyCode") != "EUR":
            return None, "RENEW priceWithTax not explicitly provided in EUR"
        try:
            total += parse_eur(gross.get("value"), label="RENEW gross", allow_zero=True)
        except PurchaseSafetyError:
            return None, "malformed RENEW gross amount"
    return total, "explicit gross item-level renewal components"


def _local_guard(journal: OrderJournal, quota: PurchaseQuota, maximum: int) -> None:
    if journal.read() is not None:
        raise PurchaseSafetyError("Legacy purchase intent unresolved; commercial audit blocked")
    quota._check_legacy()
    attempts = quota.read()
    if len(attempts) >= maximum:
        raise PurchaseSafetyError("Local two-server reservation cap reached")
    if any(item["status"] in OPEN_STATES for item in attempts):
        raise PurchaseSafetyError("Unresolved local purchase attempt blocks commercial audit")
    if attempts:
        # A local confirmed attempt and the remote service inventory may
        # describe the same server; never count them independently as proof.
        raise PurchaseSafetyError("Local purchase history requires manual cross-check")


def audit_commercial(policy: PurchasePolicy, *, stocks: list[Stock],
                     client: OVHCartClient | None, journal: OrderJournal,
                     quota: PurchaseQuota,
                     pending_check: Callable[[OVHCartClient, int], PendingOrderInventory]
                     = reconcile_pending_orders) -> str:
    """One candidate, one bounded remote scan, one temporary cart, no order."""
    if policy.enabled:
        raise PurchaseSafetyError("Live checkout is not authorized")
    if not isinstance(policy.max_monthly_eur, Decimal) or policy.max_monthly_eur <= 0:
        raise PurchaseSafetyError("Missing monthly EUR limit")
    if not stocks:
        raise PurchaseSafetyError("Stock feed incomplete; no commercial audit")
    candidate = choose_candidate(stocks, policy)
    if candidate is None:
        return "WAITING: no eligible KS-LE-6 128GB stock; no account/cart request, no order."
    _local_guard(journal, quota, policy.max_servers)
    if client is None:
        raise PurchaseSafetyError("OVHcloud API credentials required for commercial audit")

    remote = pending_check(client, policy.max_servers)
    if remote.overlap_unverified:
        raise PurchaseSafetyError("Account/pending-order identity overlap unresolved")
    if remote.upper_bound >= policy.max_servers:
        return (
            f"LIMIT REACHED: remote verified count {remote.upper_bound}/"
            f"{policy.max_servers}; no cart or order."
        )

    verify_available_test_fqn("27skle06-v1", candidate.fqn, candidate.datacenter)
    results: dict[str, Any] = {}

    def check_quote(quote: Any) -> None:
        results["initial"] = validate_initial_price(quote, policy.max_total_eur)
        try:
            results["zero_commitment"] = inspect_commitment(quote)
        except CommitmentSafetyError as exc:
            raise PurchaseSafetyError(str(exc)) from exc

    def check_renewals(items: tuple[Any, ...]) -> None:
        for item in items:
            try:
                if inspect_commitment(item):
                    results["zero_commitment"] = True
            except CommitmentSafetyError as exc:
                raise PurchaseSafetyError(str(exc)) from exc
        results["renewal"] = inspect_cart_renewals(items)
        results["gross_renewal"] = _renewal_vat_gross(items)

    test_order(
        client, datacenter=candidate.datacenter, plan_code="27skle06-v1",
        target_fqn=candidate.fqn, quote_validator=check_quote,
        renewal_observer=check_renewals,
    )
    if "initial" not in results or "renewal" not in results or "gross_renewal" not in results:
        raise PurchaseSafetyError("Incomplete temporary cart commercial evidence")
    _local_guard(journal, quota, policy.max_servers)

    initial: InitialPrice = results["initial"]
    renewal = results["renewal"]
    gross_renewal, reason = results["gross_renewal"]
    if renewal.amount_eur is None:
        monthly = (
            f"MONTHLY WITH VAT UNVERIFIED: incomplete RENEW components "
            f"({renewal.reason}); ceiling {policy.max_monthly_eur:.2f} EUR. "
        )
    elif gross_renewal is not None and gross_renewal < renewal.amount_eur:
        monthly = (
            "MONTHLY WITH VAT UNVERIFIED: gross RENEW sum lower than "
            "unqualified RENEW prices; inconsistent evidence. "
        )
    elif gross_renewal is None:
        monthly = (
            f"MONTHLY WITH VAT UNVERIFIED: {reason}; ceiling "
            f"{policy.max_monthly_eur:.2f} EUR. "
        )
    elif gross_renewal > policy.max_monthly_eur:
        monthly = (
            f"MONTHLY PRICE WARNING: item-level gross {gross_renewal:.2f} EUR "
            f"exceeds ceiling {policy.max_monthly_eur:.2f} EUR; no approval. "
        )
    else:
        monthly = (
            f"Item-level gross RENEW evidence {gross_renewal:.2f} EUR "
            f"<= {policy.max_monthly_eur:.2f} EUR; final monthly invoice "
            "and future discounts NOT VERIFIED. "
        )
    commitment = (
        "Zero-commitment indicator observed, but legal terms NOT VERIFIED. "
        if results.get("zero_commitment") else
        "No explicit zero-commitment proof; legal terms NOT VERIFIED. "
    )
    return (
        "COMMERCIAL AUDIT PARTIAL (NO PURCHASE): "
        f"{candidate.fqn} at {candidate.datacenter.upper()}; "
        f"initial net {initial.without_tax:.2f} EUR + VAT {initial.vat:.2f} EUR "
        f"= {initial.with_tax:.2f} EUR incl. VAT "
        f"(cap {policy.max_total_eur:.2f} EUR, all cart charges included). "
        "Separate installation fee NOT itemized/verified. "
        + monthly + renewal.summary() + " " + commitment
        + f"Remote service + open order count {remote.upper_bound}/"
        f"{policy.max_servers}. Checkout/payment remain DISABLED."
    )


def run_commercial_audit() -> int:
    """Manually invoked only; not attached to the 30-second stock timer."""
    try:
        policy = PurchasePolicy.from_env()
        journal = OrderJournal(Path(os.getenv("OVH_AUTO_BUY_JOURNAL", DEFAULT_JOURNAL)))
        quota = PurchaseQuota(
            Path(os.getenv("OVH_AUTO_BUY_QUOTA_JOURNAL", DEFAULT_PATH)),
            max_servers=policy.max_servers,
        )
        stocks = retrieve_stock()
        candidate = choose_candidate(stocks, policy)
        client = make_api_client() if candidate is not None else None
        print(audit_commercial(
            policy, stocks=stocks, client=client, journal=journal, quota=quota,
        ))
        return 0
    except (PurchaseSafetyError, PendingOrdersError, AccountReconciliationError,
            OrderProbeError, MonitorError, APITransientError) as exc:
        LOG.error("Commercial audit inconclusive (NO PURCHASE): %s", exc)
        return 1
