"""Non-billable OVHcloud payment and contract observations for preflight.

Do not confuse a saved default one-click method with permission, funds,
strong customer authentication, or eligibility to pay a *specific order*.
The latter would require a real order ID; this module never creates one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from order_probe import OVHCartClient, OrderProbeError
from payment_readiness import PaymentReadinessError, payment_readiness


@dataclass(frozen=True)
class PaymentEvidence:
    status: str

    @property
    def saved_default_valid(self) -> bool:
        return self.status == "VALID_DEFAULT"

    @property
    def order_payment_verified(self) -> bool:
        # A pre-order method inventory cannot check this reliably.
        return False

    def summary(self) -> str:
        descriptions = {
            "VALID_DEFAULT": (
                "READ-ONLY PAYMENT: default VALID one-click method found; "
                "order-specific payment eligibility, 3-D Secure, available "
                "funds and payment success NOT VERIFIED."
            ),
            "NO_METHOD": "PAYMENT NOT READY: no saved payment method; no payment attempted.",
            "INVALID_DEFAULT": (
                "PAYMENT NOT READY: no unambiguous valid default one-click "
                "method; no payment attempted."
            ),
            "API_UNVERIFIED": (
                "PAYMENT UNVERIFIED: GET payment method inventory unavailable "
                "or not authorized; no payment attempted."
            ),
        }
        return descriptions[self.status]


def inspect_payment_evidence(client: OVHCartClient) -> PaymentEvidence:
    """Classify read-only evidence without exposing card or account IDs."""
    try:
        payment_readiness(client)
    except PaymentReadinessError as exc:
        if str(exc) == "No registered payment methods":
            return PaymentEvidence("NO_METHOD")
        return PaymentEvidence("INVALID_DEFAULT")
    except OrderProbeError:
        return PaymentEvidence("API_UNVERIFIED")
    return PaymentEvidence("VALID_DEFAULT")


def describe_cart_contracts(quote: Any) -> str:
    """Only disclose a bounded count of contract references, never URLs/text.

    Presence of contract references is NOT proof of a zero-commitment term.
    """
    if not isinstance(quote, dict):
        return "CONTRACTS UNVERIFIED: malformed checkout quote."
    contracts = quote.get("contracts")
    if not isinstance(contracts, list):
        return "CONTRACTS UNVERIFIED: no structured contract list in checkout."
    if len(contracts) > 100:
        return "CONTRACTS UNVERIFIED: excessive contract references."
    if not contracts:
        return "CONTRACTS UNVERIFIED: checkout supplied no contract references."
    if any(not isinstance(doc, (dict, str)) for doc in contracts):
        return "CONTRACTS UNVERIFIED: malformed contract references."
    return (
        f"CONTRACT REVIEW REQUIRED: {len(contracts)} contract reference(s) "
        "in checkout; text not inspected; zero commitment NOT VERIFIED."
    )
