"""Read-only payment-method readiness check for future OVHcloud orders.

No payment or checkout calls are made. Identifiers and card details are never
included in CLI output. The credentials must have narrowly scoped GET rights.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
from typing import Any

from order_probe import OVHCartClient, OrderProbeError

LOG = logging.getLogger("ksle-payment-readiness")


class PaymentReadinessError(Exception):
    pass


def parse_expiry(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise PaymentReadinessError("Unexpected payment method expiration date")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("No timezone")
        return result.astimezone(timezone.utc)
    except ValueError as exc:
        raise PaymentReadinessError("Unexpected payment method expiration date") from exc


def analyze_payment_methods(ids: Any, details: list[Any],
                            now: datetime | None = None) -> str:
    if not isinstance(ids, list) or len(ids) > 100:
        raise PaymentReadinessError("Unexpected payment method list from OVHcloud")
    if not ids:
        raise PaymentReadinessError("No registered payment methods")
    if len(details) != len(ids):
        raise PaymentReadinessError("Incomplete payment method verification")
    defaults: list[dict] = []
    for method_id, item in zip(ids, details):
        if isinstance(method_id, bool) or not isinstance(method_id, int) or method_id < 1:
            raise PaymentReadinessError("Unexpected payment method identifier")
        if not isinstance(item, dict) or item.get("paymentMethodId") != method_id:
            raise PaymentReadinessError("Payment method lookup did not match expected identifier")
        if item.get("default") is True:
            defaults.append(item)
    if len(defaults) != 1:
        raise PaymentReadinessError("Expected exactly one default payment method")
    default = defaults[0]
    if default.get("status") != "VALID":
        raise PaymentReadinessError("Default payment method is not VALID")
    if default.get("oneclick") is not True:
        raise PaymentReadinessError("Default payment method does not indicate one-click support")
    expiration = parse_expiry(default.get("expirationDate"))
    current = datetime.now(timezone.utc) if now is None else now
    if expiration is not None and expiration <= current:
        raise PaymentReadinessError("Default payment method has expired")
    # No raw card/payment identifiers or instrument details in output.
    return (
        f"READ-ONLY: exactly one default VALID one-click payment method found "
        f"among {len(ids)} registered method(s). "
        "Payment authorization, available funds, and live checkout are NOT verified."
    )


def payment_readiness(client: OVHCartClient) -> str:
    ids = client.request("GET", "/me/payment/method")
    if not isinstance(ids, list) or len(ids) > 100:
        raise PaymentReadinessError("Invalid payment method list")
    if not all(isinstance(i, int) and not isinstance(i, bool) and 0 < i < 10**14 for i in ids):
        raise PaymentReadinessError("Invalid payment method IDs")
    if len(set(ids)) != len(ids):
        raise PaymentReadinessError("Duplicate payment method IDs")
    details = [client.request("GET", f"/me/payment/method/{method_id}") for method_id in ids]
    return analyze_payment_methods(ids, details)


def run_payment_readiness() -> int:
    try:
        from purchase_guard import make_api_client
        client = make_api_client()
        print(payment_readiness(client))
        return 0
    except (OrderProbeError, PaymentReadinessError) as exc:
        LOG.error("Read-only payment check failed (no order/payment attempted): %s", exc)
        return 1
