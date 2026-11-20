"""Redacted, conservative commercial evidence from a MANUAL test cart.

Pure formatting only: no OVH calls, no purchase guard modification, and no
checkout/payment implementation. A quote is not permission to buy.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Sequence

from cart_identity import CartIdentityEvidence, unavailable as identity_unavailable
from commitment_guard import CommitmentSafetyError, inspect_commitment
from commercial_audit import _renewal_vat_gross
from payment_evidence import describe_cart_contracts
from purchase_guard import PurchaseSafetyError, checkout_total_eur, parse_eur
from quote_breakdown import inspect_checkout_details
from renewal_pricing import inspect_cart_renewals


def _initial_vat_summary(quote: Any) -> str:
    """Only a complete, arithmetically consistent structured EUR quote passes."""
    if not isinstance(quote, dict):
        return "INITIAL VAT: UNVERIFIED; checkout did not return structured prices."
    prices = quote.get("prices")
    if not isinstance(prices, dict):
        return "INITIAL VAT: UNVERIFIED; checkout did not return structured prices."
    try:
        gross = checkout_total_eur(quote)
        def amount(field: str) -> Decimal:
            row = prices.get(field)
            if not isinstance(row, dict) or row.get("currencyCode") != "EUR":
                raise PurchaseSafetyError("Incomplete structured EUR VAT amount")
            return parse_eur(row.get("value"), label=field, allow_zero=True)
        net = amount("withoutTax")
        tax = amount("tax")
    except PurchaseSafetyError:
        return "INITIAL VAT: UNVERIFIED; missing or invalid numeric EUR net/tax/total."
    if net + tax != gross:
        return "INITIAL VAT: CONFLICT; numeric net + tax does not equal total."
    return (f"INITIAL VAT: {net:.2f} EUR net + {tax:.2f} EUR tax "
            f"= {gross:.2f} EUR with tax (current cart only).")


def manual_cart_report(quote: Any, items: Sequence[Any],
                       identity: CartIdentityEvidence | None = None) -> str:
    """Never include free-form OVH strings, URLs, cart IDs, or card details."""
    identity = identity if isinstance(identity, CartIdentityEvidence) else identity_unavailable()
    lines = ["MANUAL CART COMMERCIAL EVIDENCE (NO PURCHASE):",
             _initial_vat_summary(quote),
             inspect_checkout_details(quote).summary()]
    renewals = inspect_cart_renewals(items)
    lines.append(renewals.summary())
    gross, _ = _renewal_vat_gross(items)
    if (gross is not None and renewals.amount_eur is not None
            and gross >= renewals.amount_eur):
        lines.append(f"ITEM RENEW WITH VAT: {gross:.2f} EUR (indicative component sum; "
                     "future monthly invoice NOT VERIFIED).")
    elif gross is not None and renewals.amount_eur is not None:
        lines.append("ITEM RENEW WITH VAT: CONFLICT; gross components below unqualified RENEW sum.")
    else:
        # Intentionally display no raw provider reason (the known fixed reason
        # strings are already handled by the underlying strict parser).
        lines.append("ITEM RENEW WITH VAT: UNVERIFIED; complete tax-explicit "
                     "monthly component prices were not returned.")

    records = ([quote] if isinstance(quote, dict) else []) + [
        item for item in items if isinstance(item, dict)
    ] if isinstance(items, (list, tuple)) else []
    try:
        zero_found = inspect_commitment(*records)
    except CommitmentSafetyError:
        lines.append("NO FIXED COMMITMENT: CONFLICT; explicit long-term indicator detected.")
    else:
        lines.append(
            "NO FIXED COMMITMENT: explicit zero indicator seen, but legal text NOT VERIFIED."
            if zero_found else "NO FIXED COMMITMENT: UNVERIFIED; P1M billing alone is not proof."
        )
    lines.append(describe_cart_contracts(quote))
    lines.append(identity.summary())
    lines.append("SETUP FEES: item-level totals do not prove their VAT treatment; "
                 "a missing INSTALLATION line is not proof of zero fees.")
    lines.append("This is a diagnostic quote for the selected test model, "
                 "not a KS-LE-6 price approval. Checkout and payment remain DISABLED.")
    return "\n".join(lines)
