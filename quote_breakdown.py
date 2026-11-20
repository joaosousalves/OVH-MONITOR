"""Non-billable, privacy-safe inspection of OVH cart checkout detail lines.

The validated checkout gross total is the initial payment evidence. Detail
lines may describe installation and promotions, but their amounts are NOT
automatically VAT-inclusive and they never certify future renewals.

No network requests, payment actions, or checkout mutations exist here.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from purchase_guard import PurchaseSafetyError, parse_eur


MAX_LINES = 120
PROMO_TYPES = frozenset(("GIFT", "DISCOUNT", "REDUCTION", "PROMOTION"))


@dataclass(frozen=True)
class CartBreakdown:
    status: str
    reason: str
    installation_lines: int = 0
    installation_listed_eur: Decimal | None = None
    duration_lines: int = 0
    promotion_lines: int = 0
    other_lines: int = 0
    duration_listed_eur: Decimal | None = None

    def summary(self) -> str:
        if self.status != "LINES_PRESENT":
            return (
                "INITIAL FEE BREAKDOWN UNVERIFIED: " + self.reason
                + "; cart gross total still includes all checkout charges, "
                "but separate setup fees/VAT have not been established. "
            )
        return (
            "INITIAL CART DETAIL LINES (not proof of VAT treatment): "
            f"{self.installation_lines} INSTALLATION line(s), "
            f"listed amount {self.installation_listed_eur:.2f} EUR "
            "(tax basis unknown); "
            f"{self.duration_lines} DURATION line(s), "
            f"listed {self.duration_listed_eur:.2f} EUR "
            "(tax basis unknown; not a renewal invoice); "
            f"{self.promotion_lines} promotion line(s), "
            f"{self.other_lines} unclassified line(s). "
            "A zero or missing INSTALLATION line does not guarantee "
            "there are no separate or future setup fees. "
        )


def inspect_checkout_details(quote: Any) -> CartBreakdown:
    """Parse bounded checkout.details, never logging descriptions or URLs.

    OVH's checkout details, if supplied, have a detailType and totalPrice.
    We report only numeric aggregates and known labels; unknown shapes are
    explicitly inconclusive rather than silently treated as zero charges.
    """
    if not isinstance(quote, dict):
        return CartBreakdown("UNVERIFIED", "checkout response is not an object")
    details = quote.get("details")
    if not isinstance(details, list):
        return CartBreakdown("UNVERIFIED", "checkout did not include structured detail lines")
    if not details or len(details) > MAX_LINES:
        return CartBreakdown("UNVERIFIED", "checkout detail lines are empty or unexpectedly numerous")

    setup = Decimal("0.00")
    duration_total = Decimal("0.00")
    installs = durations = promos = others = 0
    for row in details:
        if not isinstance(row, dict):
            return CartBreakdown("UNVERIFIED", "malformed checkout detail line")
        kind = row.get("detailType")
        if not isinstance(kind, str) or not 0 < len(kind) <= 64:
            return CartBreakdown("UNVERIFIED", "missing or invalid checkout line category")
        price = row.get("totalPrice")
        if not isinstance(price, dict) or price.get("currencyCode") != "EUR":
            return CartBreakdown("UNVERIFIED", "line total lacks a structured EUR currency")
        try:
            amount = parse_eur(price.get("value"), label="checkout detail price", allow_zero=True)
        except PurchaseSafetyError:
            return CartBreakdown("UNVERIFIED", "invalid or negative checkout detail amount")
        if kind == "INSTALLATION":
            installs += 1
            setup += amount
        elif kind == "DURATION":
            durations += 1
            duration_total += amount
        elif kind in PROMO_TYPES:
            promos += 1
        else:
            others += 1
    return CartBreakdown(
        "LINES_PRESENT", "checkout returned structured EUR detail lines",
        installs, setup, durations, promos, others, duration_total,
    )
