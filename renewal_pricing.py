"""Conservative, non-authoritative renewal evidence from temporary Eco cart items.

The OVH item API can include a RENEW price label.  This is useful evidence,
but the label does not attest to VAT, future discounts or a final renewal bill.
Never use it to approve a purchase against a VAT-inclusive monthly ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Sequence


@dataclass(frozen=True)
class RenewalEvidence:
    amount_eur: Decimal | None
    components: int
    reason: str

    def summary(self) -> str:
        if self.amount_eur is None:
            return ("Cart RENEW evidence unavailable/incomplete "
                    f"({self.reason}); VAT-inclusive monthly price NOT VERIFIED.")
        return (
            f"Cart RENEW component sum {self.amount_eur:.2f} EUR "
            f"({self.components} item(s)); VAT status, discounts, and final "
            "monthly renewal NOT VERIFIED. This is NOT a checkout renewal quote."
        )


def _parse_eur(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    string = str(value)
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,2})?", string):
        return None
    try:
        return Decimal(string).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def inspect_cart_renewals(items: Sequence[Any]) -> RenewalEvidence:
    """Require one unambiguous EUR RENEW row per added cart component.

    All requests used to create these components explicitly request P1M.
    If a returned duration disagrees, refuse to sum its prices. A complete
    sum is still *not* proof of monthly VAT-inclusive renewal pricing.
    """
    if not isinstance(items, (list, tuple)) or not items:
        return RenewalEvidence(None, 0, "no item responses")
    total = Decimal("0.00")
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            return RenewalEvidence(None, index - 1, "item response is not an object")
        if item.get("duration", "P1M") != "P1M":
            return RenewalEvidence(None, index - 1, "non-monthly item duration")
        rows = item.get("prices")
        if not isinstance(rows, list):
            return RenewalEvidence(None, index - 1, "missing item price labels")
        renewals = [row for row in rows if isinstance(row, dict) and row.get("label") == "RENEW"]
        if len(renewals) != 1:
            return RenewalEvidence(None, index - 1, "missing or ambiguous RENEW label")
        price = renewals[0].get("price")
        if not isinstance(price, dict) or price.get("currencyCode") != "EUR":
            return RenewalEvidence(None, index - 1, "RENEW currency not explicitly EUR")
        amount = _parse_eur(price.get("value"))
        if amount is None:
            return RenewalEvidence(None, index - 1, "invalid RENEW EUR amount")
        total += amount
    return RenewalEvidence(total, len(items), "complete item price labels")
