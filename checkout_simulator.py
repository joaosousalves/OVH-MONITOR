"""Offline checkout lifecycle simulator and read-only order reconciliation.

There is deliberately NO live POST /checkout implementation. The simulator
uses only a local FakeGateway and temporary ledger. It never reads API
credentials, uses the network or charges a payment method.
"""
from __future__ import annotations

from decimal import Decimal
import logging
import os
from pathlib import Path
import tempfile
from typing import Any

from purchase_guard import (
    OrderJournal, PurchasePolicy, PurchaseSafetyError, EXPECTED_FQN,\n    DEFAULT_JOURNAL, make_api_client,
)
from order_probe import OVHCartClient, OrderProbeError

LOG = logging.getLogger("ksle-checkout-simulator")
SIMULATED_TOTAL = Decimal("39.99")
SIMULATED_ORDER_ID = 123456789


class SimulationError(Exception):
    pass


class FakeGateway:
    """Fixed offline API response, never an OVHcloud network adapter."""

    def __init__(self, scenario: str):
        if scenario not in ("accepted", "timeout", "rejected"):
            raise SimulationError("Unsupported simulation scenario")
        self.scenario = scenario
        self.calls = 0

    def submit(self, details: dict[str, Any]) -> int:
        self.calls += 1
        if details.get("autoPayWithPreferredPaymentMethod") is not True:
            raise SimulationError("Autopay must be explicitly enabled in the fake payload")
        if details.get("waiveRetractationPeriod") is not False:
            raise SimulationError("Unexpected retraction-period waiver")
        if self.scenario == "timeout":
            raise TimeoutError("SIMULATED network timeout with unknown order outcome")
        if self.scenario == "rejected":
            raise SimulationError("SIMULATED checkout rejection")
        return SIMULATED_ORDER_ID


def notification_text(*, status: str, datacenter: str, amount: Decimal,
                      order_id: int | None = None) -> tuple[str, str]:
    """Notification payload only; no email is sent by simulator."""
    if status not in ("submitted", "unknown"):
        raise SimulationError("Invalid notification state")
    subject = f"[SIMULATION ONLY] KS-LE-6 order {status.upper()}"
    identifier = str(order_id) if order_id is not None else "NOT KNOWN"
    body = (
        "SIMULATION ONLY — no OVHcloud order or charge was made.\n"
        f"Product: KS-LE-6 / {EXPECTED_FQN}\n"
        f"Datacenter: {datacenter.upper()}\n"
        f"Example initial total incl VAT: {amount:.2f} EUR\n"
        f"Outcome: {status}\n"
        f"Mock order reference: {identifier}\n"
        "The journal is locked after a simulated attempt, including timeout.\n"
        "Real status must be reconciled with OVHcloud before retrying.\n"
    )
    return subject, body


def simulate_checkout(scenario: str, policy: PurchasePolicy,
                      *, example_amount: Decimal = SIMULATED_TOTAL) -> str:
    """Demonstrate one-shot commit, outcome tracking and notification formatting.

    This MUST ONLY use FakeGateway with a TemporaryDirectory. Adding a real
    transport to this function would break its safety invariant.
    """
    if policy.enabled:
        raise SimulationError("Offline simulator requires purchase to be disabled")
    if not isinstance(example_amount, Decimal) or not example_amount.is_finite():
        raise SimulationError("Invalid simulation price")
    if example_amount <= 0 or example_amount > policy.max_total_eur:
        raise SimulationError("SIMULATED quote exceeds configured initial-charge cap")
    if not policy.datacenters:
        raise SimulationError("No configured datacenter")
    gateway = FakeGateway(scenario)
    dc = policy.datacenters[0]
    with tempfile.TemporaryDirectory(prefix="ksle-offline-") as directory:
        journal = OrderJournal(Path(directory) / "intent.json")
        if not journal.reserve_once(fqn=EXPECTED_FQN, datacenter=dc, total=example_amount):
            raise SimulationError("SIMULATION duplicate reservation rejected")
        payload = {
            "autoPayWithPreferredPaymentMethod": True,
            "waiveRetractationPeriod": False,
        }
        try:
            order_id = gateway.submit(payload)
        except (TimeoutError, SimulationError):
            # Even a definitive-looking error is treated as uncertain:
            # never blindly resubmit after the API may have received the POST.
            journal.record_outcome("unknown")
            status = "unknown"
            order_id = None
        else:
            journal.record_outcome("submitted", order_id=order_id)
            status = "submitted"
        if journal.reserve_once(fqn=EXPECTED_FQN, datacenter=dc, total=example_amount):
            raise SimulationError("Idempotency violated: repeat was allowed")
        if gateway.calls != 1:
            raise SimulationError("Idempotency violated: multiple submissions")
        saved = journal.read()
        if saved is None or saved.get("status") != status:
            raise SimulationError("Journal result not durable")
        if status == "submitted" and saved.get("orderId") != order_id:
            raise SimulationError("Simulated order identifier not recorded")
        subject, _body = notification_text(status=status, datacenter=dc,
                                            amount=example_amount, order_id=order_id)
        return (
            f"{subject}. Example initial total: {example_amount:.2f} EUR; "
            f"status={status}; repeat blocked. "
            "Offline fake API only, no email dispatched, no real order."
        )


def read_only_order_status(client: OVHCartClient, journal: OrderJournal) -> str:
    """Query OVHcloud order status only when a known orderId exists in journal.

    Unknown outcomes with no orderId require manual investigation; no attempt
    to infer that a missing ID means no purchase succeeded.
    """
    record = journal.read()
    if record is None:
        return "No order journal. No order status to reconcile."
    order_id = record.get("orderId")
    if type(order_id) is not int or not 0 < order_id < 10**15:
        return (
            "Journal contains an attempted or uncertain purchase without "
            "a verified order ID. Investigate OVHcloud manually; do not retry."
        )
    response = client.request("GET", f"/me/order/{order_id}/status")
    if isinstance(response, str):
        status = response
    elif isinstance(response, dict):
        status = response.get("status")
    else:
        raise SimulationError("Unexpected order status response")
    if not isinstance(status, str) or not status or len(status) > 40 or not status.replace("_", "").isalnum():
        raise SimulationError("Invalid order status from OVHcloud")
    # This is a status observation, NOT proof of payment or delivery.
    # Never modify, delete or unlock the journal from a GET request.
    return f"READ-ONLY: recorded order status is {status}; journal remains locked."


def run_checkout_simulation(scenario: str = "accepted") -> int:
    try:
        policy = PurchasePolicy.from_env()
        print(simulate_checkout(scenario, policy))
        return 0
    except (SimulationError, PurchaseSafetyError) as exc:
        LOG.error("Checkout simulation blocked; no order submitted: %s", exc)
        return 1


def run_order_reconciliation() -> int:
    """Read-only command for reviewing a recorded order, never unlocking it."""
    try:
        journal = OrderJournal(Path(os.getenv("OVH_AUTO_BUY_JOURNAL", DEFAULT_JOURNAL)))
        record = journal.read()
        if record is None:
            print("No purchase journal exists; no order reconciliation is required.")
            return 0
        if not isinstance(record.get("orderId"), int):
            print("UNCERTAIN: journal has no order ID. Manual OVHcloud account "
                  "review required; retry blocked.")
            return 1
        client = make_api_client()
        print(read_only_order_status(client, journal))
        return 0
    except (SimulationError, PurchaseSafetyError, OrderProbeError) as exc:
        LOG.error("Read-only order status check failed; no order submitted: %s", exc)
        return 1
