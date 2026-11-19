"""Manual KS-LE-6 purchase preflight: quote + payment readiness, never order.

This phase deliberately reuses the temporary-cart GET checkout preview and
the read-only payment check. It has NO POST checkout or payment transport and
creates no purchase reservation. Never schedule it on the stock timer.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from ksle_monitor import APITransientError, MonitorError, Stock, retrieve_stock
from order_probe import OVHCartClient, OrderProbeError
from payment_readiness import PaymentReadinessError, payment_readiness
from purchase_guard import (
    DEFAULT_JOURNAL, OrderJournal, PurchasePolicy, PurchaseSafetyError,
    choose_candidate, make_api_client, preview,
)
from purchase_quota import DEFAULT_PATH, PurchaseQuota


LOG = logging.getLogger("ksle-purchase-preflight")


def preflight(policy: PurchasePolicy, *, stocks: list[Stock],
              client: OVHCartClient | None, journal: OrderJournal,
              quota: PurchaseQuota,
              payment_check: Callable[[OVHCartClient], str] = payment_readiness) -> str:
    """Return an explanatory preview, without reserving or placing orders.

    All stock, legacy-ledger, duplicate-attempt, FQN and price checks are
    performed by purchase_guard.preview, before payment readiness is queried.
    Payment readiness does NOT prove sufficient funds or charge success.
    """
    if policy.enabled:
        raise PurchaseSafetyError("Live checkout is not authorized")

    quote_report = preview(policy, stocks=stocks, client=client,
                           journal=journal, quota=quota)
    if not quote_report.startswith("PREVIEW ONLY:"):
        return quote_report + " Payment not checked; no order submitted."

    choice = choose_candidate(stocks, policy)
    if choice is None or client is None:
        raise PurchaseSafetyError("Preflight cannot verify an eligible cart")
    payment_report = payment_check(client)
    # Re-read the local quota because an external process may have reserved
    # a slot while a temporary cart was being quoted. Still no reservation.
    quota._check_legacy()
    attempts = quota.read()
    if len(attempts) >= policy.max_servers:
        return "LIMIT REACHED while checking payment; no order submitted."
    if any(record["status"] in ("reserved", "submitted", "unknown")
           for record in attempts):
        raise PurchaseSafetyError("Another purchase remains unresolved")

    disk_approval = (
        "Disk configuration already on the future checkout approval list."
        if choice.fqn in quota.approved else
        "REVIEW REQUIRED: this alternative disk configuration is preview-only "
        "and is NOT authorized for future checkout."
    )
    remaining = policy.max_servers - len(attempts)
    return (
        f"PREFLIGHT ONLY (NO PURCHASE): {quote_report} "
        f"Payment check: {payment_report} "
        f"{remaining}/{policy.max_servers} local reservation slots remain. "
        f"{disk_approval} The account-wide order count, recurring monthly "
        "cost and payment success are NOT verified."
    )


def run_purchase_preflight() -> int:
    """One-shot manual command, never attached to the monitoring timer."""
    try:
        policy = PurchasePolicy.from_env()
        journal = OrderJournal(Path(os.getenv("OVH_AUTO_BUY_JOURNAL", DEFAULT_JOURNAL)))
        quota = PurchaseQuota(
            Path(os.getenv("OVH_AUTO_BUY_QUOTA_JOURNAL", DEFAULT_PATH)),
            max_servers=policy.max_servers,
        )
        stocks = retrieve_stock()
        candidate = choose_candidate(stocks, policy)
        # No remote account or temporary-cart calls when stock is absent.
        client = make_api_client() if candidate is not None else None
        print(preflight(policy, stocks=stocks, client=client,
                        journal=journal, quota=quota))
        return 0
    except (PurchaseSafetyError, PaymentReadinessError, OrderProbeError,
            MonitorError, APITransientError) as exc:
        LOG.error("Purchase preflight blocked (no order submitted): %s", exc)
        return 1
