"""End-to-end, OFFLINE KS-LE-6 two-order rehearsal with real safety gates.

No OVH client, network transport, email sender, real checkout endpoint or
payment operation exists here. The only accepted gateway is the built-in
FakeCheckoutGateway. Quota files live inside a disposable TemporaryDirectory.
Even a fully passing rehearsal never authorizes a live order.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import tempfile

from auto_buy_guard import PurchaseEvidence, assess_auto_buy
from confirmed_terms import for_storage
from ksle_monitor import Stock
from purchase_guard import (
    APPROVED_FQNS, STORAGE_PRIORITIES, PurchasePolicy,
    PurchaseSafetyError, choose_candidate,
)
from purchase_quota import OPEN_STATES, PurchaseQuota


class FakeCheckoutError(Exception):
    """Only a simulated checkout failure; never an OVH error."""


class FakeCheckoutGateway:
    """Intentionally cannot be configured with a real base URL or API key."""

    def __init__(self, scenario: str = "accepted", order_id: int = 1001):
        if scenario not in ("accepted", "timeout", "rejected"):
            raise ValueError("Unsupported simulated outcome")
        if type(order_id) is not int or order_id <= 0:
            raise ValueError("Fake order ID must be positive")
        self.scenario = scenario
        self.order_id = order_id
        self.calls = 0

    def submit(self, payload: dict) -> int:
        self.calls += 1
        # This is an INTERNAL fake payload, not the OVH API checkout schema.
        if (payload.get("planCode") != "27skle06-v1"
                or payload.get("fqn") not in APPROVED_FQNS
                or payload.get("duration") != "P1M"
                or payload.get("pricingMode") != "default"
                or payload.get("quantity") != 1
                or payload.get("autoPayWithPreferredPaymentMethod") is not True
                or payload.get("waiveRetractationPeriod") is not False):
            raise FakeCheckoutError("Simulated payload violates purchase requirements")
        if self.scenario == "timeout":
            raise TimeoutError("SIMULATED timeout; the outcome is unknown")
        if self.scenario == "rejected":
            raise FakeCheckoutError("SIMULATED rejection; conservative unknown outcome")
        return self.order_id


@dataclass(frozen=True)
class FakeResult:
    slot: int
    status: str
    order_id: int | None


def fake_verified_evidence(storage: str, used: int = 0,
                           dc: str = "bhs") -> PurchaseEvidence:
    """Manufactured proof; must never be used with a real order gateway."""
    reference = for_storage(storage)
    return PurchaseEvidence(
        initial_including_vat=reference.first_payment_gross,
        initial_quote_verified=True,
        monthly_including_vat=reference.monthly_gross,
        monthly_renewal_verified=True,
        no_fixed_commitment_verified=True,
        payment_method_ready=True,
        remote_slots_used=used,
        remote_inventory_verified=True,
        local_purchase_history_clear=True,
        quote_fqn=f"27skle06-v1.ram-128g-ddr4-ecc-2400.{storage}",
        quote_datacenter=dc,
        quote_identity_verified=True,
    )


def attempt_fake_purchase(policy: PurchasePolicy, stocks: list[Stock],
                          evidence: PurchaseEvidence, quota: PurchaseQuota,
                          gateway: FakeCheckoutGateway) -> FakeResult:
    """Preflight -> local reservation -> one fake submission -> durable result.

    This function refuses any non-FakeCheckoutGateway object to prevent a
    caller from substituting a network-backed adapter accidentally.
    """
    if type(gateway) is not FakeCheckoutGateway or policy.enabled:
        raise PurchaseSafetyError("Only offline simulated purchase is supported")
    candidate = choose_candidate(stocks, policy)
    decision = assess_auto_buy(policy, candidate, evidence)
    if not decision.simulated_criteria_met:
        raise PurchaseSafetyError("SIMULATION BLOCKED: " + "; ".join(decision.blockers))
    if candidate is None or evidence.initial_including_vat is None:
        raise PurchaseSafetyError("SIMULATION BLOCKED: incomplete selection or quote")

    # The fake remote count must never be less than this machine's known
    # confirmed orders. A pending/unknown local order always blocks retry.
    quota._check_legacy()
    existing = quota.read()
    if any(row["status"] in OPEN_STATES for row in existing):
        raise PurchaseSafetyError("SIMULATION BLOCKED: previous order unresolved")
    if len(existing) > evidence.remote_slots_used:
        raise PurchaseSafetyError("SIMULATION BLOCKED: stale remote count")
    slot = quota.reserve(
        fqn=candidate.fqn, datacenter=candidate.datacenter,
        total=evidence.initial_including_vat,
    )
    if slot is None:
        raise PurchaseSafetyError("SIMULATION BLOCKED: two-server local quota reached")

    payload = {
        "planCode": "27skle06-v1", "fqn": candidate.fqn,
        "datacenter": candidate.datacenter, "quantity": 1,
        "duration": "P1M", "pricingMode": "default",
        "autoPayWithPreferredPaymentMethod": True,
        "waiveRetractationPeriod": False,
    }
    try:
        order_id = gateway.submit(payload)
    except (TimeoutError, FakeCheckoutError):
        # Even an apparent rejection might have crossed the network in a
        # real system; never make an automatic second attempt.
        quota.update_outcome(slot=slot, status="unknown")
        return FakeResult(slot, "unknown", None)
    quota.update_outcome(slot=slot, status="submitted", order_id=order_id)
    return FakeResult(slot, "submitted", order_id)


def _policy() -> PurchasePolicy:
    return PurchasePolicy.from_env({
        "OVH_AUTO_BUY_ENABLED": "false",
        "OVH_AUTO_BUY_MAX_TOTAL_EUR": "80.00",
        "OVH_AUTO_BUY_MAX_MONTHLY_EUR": "37.00",
        "OVH_AUTO_BUY_MAX_SERVERS": "2",
    })


def _stock(index: int, dc: str = "bhs") -> Stock:
    return Stock(APPROVED_FQNS[index], STORAGE_PRIORITIES[index], dc, "1H-low")


def simulate_safe_purchase_flow() -> list[str]:
    """Exercise two mixed orders, two identical orders, and failure recovery."""
    policy = _policy()
    lines = ["[SIMULATION ONLY] Offline fake proof, API and ledger; 0 real orders/emails/payments."]
    with tempfile.TemporaryDirectory(prefix="ksle-two-orders-offline-") as directory:
        quota = PurchaseQuota(Path(directory) / "mixed.json")
        first_gateway = FakeCheckoutGateway(order_id=1001)
        first = attempt_fake_purchase(
            policy, [_stock(1, "fra"), _stock(0, "bhs")],
            fake_verified_evidence(STORAGE_PRIORITIES[0], 0), quota, first_gateway,
        )
        assert first.status == "submitted" and first.slot == 1
        try:
            attempt_fake_purchase(
                policy, [_stock(1, "fra")], fake_verified_evidence(STORAGE_PRIORITIES[1], 0, "fra"),
                quota, FakeCheckoutGateway(order_id=1002),
            )
        except PurchaseSafetyError:
            pass
        else:
            raise AssertionError("A second order was allowed before reconciliation")
        assert len(quota.read()) == 1 and first_gateway.calls == 1
        lines.append("1. Priority #1 selected; fake order #1 submitted once; no second before reconciliation.")

        # Only in this fake simulation: external provisioning was checked.
        quota.update_outcome(slot=1, status="confirmed", order_id=first.order_id)
        second_gateway = FakeCheckoutGateway(order_id=1002)
        second = attempt_fake_purchase(
            policy, [_stock(1, "fra")], fake_verified_evidence(STORAGE_PRIORITIES[1], 1, "fra"),
            quota, second_gateway,
        )
        assert second.status == "submitted" and second.slot == 2
        assert quota.read()[1]["fqn"] != quota.read()[0]["fqn"]
        assert second_gateway.calls == 1
        lines.append("2. After simulated external reconciliation, disk #2 selected: mixed disks 2/2.")

        try:
            attempt_fake_purchase(
                policy, [_stock(2, "waw")], fake_verified_evidence(STORAGE_PRIORITIES[2], 2, "waw"),
                quota, FakeCheckoutGateway(order_id=1003),
            )
        except PurchaseSafetyError:
            pass
        else:
            raise AssertionError("Third simulated order was not blocked")
        lines.append("3. Third order blocked at global quota 2/2.")

        # Separate journal demonstrates identical disks are permitted.
        identical = PurchaseQuota(Path(directory) / "identical.json")
        once = attempt_fake_purchase(
            policy, [_stock(0)], fake_verified_evidence(STORAGE_PRIORITIES[0], 0),
            identical, FakeCheckoutGateway(order_id=2001),
        )
        identical.update_outcome(slot=once.slot, status="confirmed", order_id=once.order_id)
        twice = attempt_fake_purchase(
            policy, [_stock(0)], fake_verified_evidence(STORAGE_PRIORITIES[0], 1),
            identical, FakeCheckoutGateway(order_id=2002),
        )
        assert twice.slot == 2
        assert identical.read()[0]["fqn"] == identical.read()[1]["fqn"]
        lines.append("4. Two identical KS-LE-6 disks also allowed in one fake global quota.")

        uncertain = PurchaseQuota(Path(directory) / "uncertain.json")
        timeout_gateway = FakeCheckoutGateway("timeout", order_id=3001)
        result = attempt_fake_purchase(
            policy, [_stock(0)], fake_verified_evidence(STORAGE_PRIORITIES[0], 0),
            uncertain, timeout_gateway,
        )
        assert result.status == "unknown" and timeout_gateway.calls == 1
        try:
            attempt_fake_purchase(
                policy, [_stock(0)], fake_verified_evidence(STORAGE_PRIORITIES[0], 0),
                uncertain, FakeCheckoutGateway(order_id=3002),
            )
        except PurchaseSafetyError:
            pass
        else:
            raise AssertionError("Timeout allowed a duplicate fake order")
        lines.append("5. Timeout persists UNKNOWN; retry blocked across simulated restart.")

    lines.append("PASS: all offline safety checks; no OVH request, email or payment; live checkout disabled.")
    return lines


def run_safe_purchase_simulation() -> int:
    for line in simulate_safe_purchase_flow():
        print(line)
    return 0
