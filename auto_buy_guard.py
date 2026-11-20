"""Pure, fail-closed decision engine for a FUTURE KS-LE-6 auto-purchaser.

This module has no OVH HTTP client, payment, checkout or filesystem writes.
Its output is a dry-run explanation; passing all commercial tests would
still not grant authority to create a real purchase order.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ksle_monitor import Stock
from purchase_guard import (
    APPROVED_FQNS, DATACENTERS, PurchasePolicy, STORAGE_PRIORITIES,
)


@dataclass(frozen=True)
class PurchaseEvidence:
    """Only independently verified facts may set the proof flags to True."""

    initial_including_vat: Decimal | None = None
    initial_quote_verified: bool = False
    monthly_including_vat: Decimal | None = None
    monthly_renewal_verified: bool = False
    no_fixed_commitment_verified: bool = False
    payment_method_ready: bool = False
    remote_slots_used: int | None = None
    remote_inventory_verified: bool = False
    local_purchase_history_clear: bool = False


@dataclass(frozen=True)
class PurchaseDecision:
    candidate: Stock | None
    blockers: tuple[str, ...]
    remaining_slots: int | None

    @property
    def simulated_criteria_met(self) -> bool:
        return not self.blockers

    def summary(self) -> str:
        if self.simulated_criteria_met:
            return (
                "AUTO-BUY DRY RUN: all supplied proof conditions met, "
                f"{self.remaining_slots} verified slot(s) free; "
                "NO REAL CHECKOUT IMPLEMENTED OR AUTHORIZED."
            )
        return (
            "AUTO-BUY BLOCKED (NO PURCHASE): " + "; ".join(self.blockers)
            + ". Checkout/payment disabled."
        )


def _amount_is_valid(value: object) -> bool:
    return (type(value) is Decimal and value.is_finite()
            and value > 0 and value == value.quantize(Decimal("0.01")))


def assess_auto_buy(policy: PurchasePolicy, candidate: Stock | None,
                    evidence: PurchaseEvidence) -> PurchaseDecision:
    """Evaluate evidence for ONE server; re-evaluate after each real order.

    A stock row does not establish that two physical servers are available.
    No caller may infer authorization from simulated_criteria_met.
    """
    problems: list[str] = []
    if policy.enabled:
        problems.append("live purchase switch is not authorized")
    if candidate is None or not candidate.available:
        problems.append("eligible stock not confirmed")
    elif (candidate.fqn not in APPROVED_FQNS
          or candidate.storage not in STORAGE_PRIORITIES
          or candidate.fqn != APPROVED_FQNS[STORAGE_PRIORITIES.index(candidate.storage)]
          or candidate.datacenter not in DATACENTERS
          or candidate.datacenter not in policy.datacenters):
        problems.append("model/RAM/disks/datacenter not approved")

    if (not evidence.remote_inventory_verified or
            type(evidence.remote_slots_used) is not int or
            not 0 <= evidence.remote_slots_used <= policy.max_servers):
        remaining = None
        problems.append("remote service and pending-order quota not verified")
    else:
        remaining = policy.max_servers - evidence.remote_slots_used
        if remaining == 0:
            problems.append("server quota reached")
    if not evidence.local_purchase_history_clear:
        problems.append("local purchase ledger not reconciled")

    if (not evidence.initial_quote_verified or
            not _amount_is_valid(evidence.initial_including_vat)):
        problems.append("initial EUR amount including VAT not verified")
    elif evidence.initial_including_vat > policy.max_total_eur:
        problems.append("initial EUR amount exceeds configured per-server limit")

    if (not evidence.monthly_renewal_verified or
            not _amount_is_valid(evidence.monthly_including_vat) or
            policy.max_monthly_eur is None):
        problems.append("final monthly renewal including VAT not verified")
    elif evidence.monthly_including_vat > policy.max_monthly_eur:
        problems.append("monthly EUR amount exceeds configured per-server limit")

    if not evidence.no_fixed_commitment_verified:
        problems.append("contractual freedom from 12/24-month commitment not verified")
    if not evidence.payment_method_ready:
        problems.append("eligible payment method not verified")

    return PurchaseDecision(candidate, tuple(problems), remaining)
