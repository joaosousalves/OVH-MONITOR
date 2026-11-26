"""Owner-confirmed KS-LE-6 reference prices; never a live checkout approval.

These 2026-11-20 prices assume the PT subsidiary, 23% VAT, one month paid
upfront and 26.99 EUR ex-VAT installation. The owner selected the
month-to-month offer without a 12/24-month commitment. A temporary cart
must still be checked independently because offers can change.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from ksle_monitor import STORAGE_NAMES, STORAGE_PRIORITIES


VAT = Decimal("0.23")
SETUP_NET = Decimal("26.99")
MONTHLY_CAP = Decimal("37.00")
INITIAL_CAP = Decimal("90.00")
NET_MONTHLY_BY_STORAGE = {
    "hybridsoftraid-2x4000sa-2x500nvme": Decimal("26.99"),
    "softraid-4x960ssd": Decimal("27.99"),
    "softraid-3x1000nvme": Decimal("28.99"),
    "softraid-2x1920nvme": Decimal("29.99"),
}


def gross(net: Decimal) -> Decimal:
    return (net * (Decimal("1") + VAT)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )


@dataclass(frozen=True)
class ConfirmedTerms:
    storage: str
    monthly_net: Decimal

    @property
    def monthly_gross(self) -> Decimal:
        return gross(self.monthly_net)

    @property
    def first_payment_gross(self) -> Decimal:
        return self.monthly_gross + gross(SETUP_NET)


def for_storage(storage: str) -> ConfirmedTerms:
    if storage not in NET_MONTHLY_BY_STORAGE:
        raise ValueError("Storage configuration is not owner-approved")
    return ConfirmedTerms(storage, NET_MONTHLY_BY_STORAGE[storage])


def compare_cart(terms: ConfirmedTerms, *, initial_gross: Decimal,
                 monthly_gross: Decimal | None) -> str:
    """Display price drift, including changes still below the absolute caps.

    This never upgrades an item-level RENEW label to a binding monthly quote,
    and never treats the owner's price confirmation as contract verification.
    """
    if (type(initial_gross) is not Decimal or not initial_gross.is_finite()
            or initial_gross <= 0):
        raise ValueError("Invalid initial quote for comparison")
    if monthly_gross is not None and (
            type(monthly_gross) is not Decimal or not monthly_gross.is_finite()
            or monthly_gross <= 0):
        raise ValueError("Invalid renewal quote for comparison")
    initial_status = (
        "ABOVE_CONFIRMED_REFERENCE" if initial_gross > terms.first_payment_gross
        else "NOT_ABOVE_REFERENCE"
    )
    monthly_status = (
        "UNVERIFIED" if monthly_gross is None
        else "ABOVE_CONFIRMED_REFERENCE" if monthly_gross > terms.monthly_gross
        else "NOT_ABOVE_REFERENCE"
    )
    return (
        "OWNER-CONFIRMED PRICE COMPARISON (still NO PURCHASE): "
        f"expected <= {terms.monthly_gross:.2f} EUR/month incl. VAT and "
        f"<= {terms.first_payment_gross:.2f} EUR initial incl. VAT "
        "(one month + setup, no extras); "
        f"INITIAL={initial_status}; MONTHLY={monthly_status}. "
        "A price increase, even below the absolute caps, requires renewed "
        "owner review. No fixed-commitment or final renewal proof is inferred. "
    )


def list_confirmed_terms() -> str:
    """Fully offline price baseline; neither stock, credentials nor cart used."""
    if set(NET_MONTHLY_BY_STORAGE) != set(STORAGE_PRIORITIES):
        raise ValueError("Price references do not cover the four approved disk layouts")
    lines = [
        "OWNER-CONFIRMED KS-LE-6 / 128 GB: Portugal, 23% VAT, no fixed commitment",
        f"Installation: {SETUP_NET:.2f} EUR net / {gross(SETUP_NET):.2f} EUR incl. VAT",
    ]
    for rank, storage in enumerate(STORAGE_PRIORITIES, 1):
        terms = for_storage(storage)
        status = (
            "WITHIN LIMITS" if terms.monthly_gross <= MONTHLY_CAP
            and terms.first_payment_gross <= INITIAL_CAP else "OUTSIDE LIMITS"
        )
        lines.append(
            f"{rank}. {STORAGE_NAMES[storage]}: "
            f"{terms.monthly_gross:.2f} EUR/month incl. VAT; "
            f"{terms.first_payment_gross:.2f} EUR initial incl. VAT; {status}"
        )
    lines.append(
        "Owner-confirmed reference only; fresh OVH cart and contractual "
        "conditions must still be checked. Checkout/payment DISABLED."
    )
    return "\n".join(lines)
