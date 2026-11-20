"""Manual, GET-only OVHcloud account service inventory for KS-LE-6.

This does not reserve stock, mutate local purchase journals, or claim that
pending/unpaid orders are fully covered by the service list. All returned
service identifiers and account details stay out of logs and CLI output.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from order_probe import OVHCartClient, OrderProbeError


LOG = logging.getLogger("ksle-account-inventory")
PLAN_ALIASES = frozenset(("27skle06-v1", "27skle06"))
MAX_IDS = 250  # Manual mode only: refuse unexpectedly large account scans.


class AccountReconciliationError(Exception):
    """Unknown inventory state. Never interpret this as zero owned servers."""


@dataclass(frozen=True)
class AccountInventory:
    matching_services: int
    inspected_services: int
    max_servers: int

    @property
    def at_limit(self) -> bool:
        return self.matching_services >= self.max_servers

    def summary(self) -> str:
        return (
            f"ACCOUNT READ-ONLY: {self.matching_services} KS-LE-6 service(s) "
            f"found across {self.inspected_services} service(s) inspected; "
            f"cap {self.max_servers}; "
            + ("LIMIT REACHED. " if self.at_limit else "Below remote service cap. ")
            + "Conservative count includes expired service records. "
            "Pending orders not yet listed as services may exist; this is NOT "
            "complete account-order reconciliation or checkout permission."
        )


def validate_service_ids(ids: Any) -> list[int]:
    if not isinstance(ids, list) or len(ids) > MAX_IDS:
        raise AccountReconciliationError("Service list missing or exceeds bounded manual scan")
    if any(type(i) is not int or i <= 0 or i >= 10**15 for i in ids):
        raise AccountReconciliationError("Invalid service ID in account inventory")
    if len(set(ids)) != len(ids):
        raise AccountReconciliationError("Duplicate service IDs in account inventory")
    return ids


def analyze_services(ids: Any, details: list[Any], max_servers: int = 2) -> AccountInventory:
    valid_ids = validate_service_ids(ids)
    if type(max_servers) is not int or max_servers not in (1, 2):
        raise AccountReconciliationError("Invalid purchase quota")
    if not isinstance(details, list) or len(details) != len(valid_ids):
        raise AccountReconciliationError("Incomplete account inventory: no zero-count assumption")
    total = 0
    for detail in details:
        if not isinstance(detail, dict) or not isinstance(detail.get("plan"), dict):
            raise AccountReconciliationError("Incomplete service plan metadata")
        code = detail["plan"].get("code")
        if not isinstance(code, str) or not code:
            raise AccountReconciliationError("Missing service plan code")
        # All service IDs must be inspected; uncertain KS-LE-6 spellings
        # must not be silently excluded from the owner-wide quota.
        normalized = code.lower().replace("-", "")
        if code.lower() in PLAN_ALIASES:
            quantity = detail.get("quantity")
            if type(quantity) is not int or not 0 < quantity <= 100:
                raise AccountReconciliationError("Invalid KS-LE-6 service quantity")
            total += quantity
        elif any(token in normalized for token in ("skle06", "ksle06", "ksle6")):
            raise AccountReconciliationError("Ambiguous KS-LE-6 plan code; manual reconciliation required")
    return AccountInventory(total, len(valid_ids), max_servers)


def reconcile_account(client: OVHCartClient, max_servers: int = 2) -> AccountInventory:
    """Authenticated GET /service + GET /service/{id}; no order/cart mutation."""
    ids = validate_service_ids(client.request("GET", "/service"))
    details = [client.request("GET", f"/service/{service_id}") for service_id in ids]
    return analyze_services(ids, details, max_servers)


def run_account_reconciliation() -> int:
    try:
        from purchase_guard import PurchasePolicy, make_api_client
        policy = PurchasePolicy.from_env()
        print(reconcile_account(make_api_client(), policy.max_servers).summary())
        return 0
    except (OrderProbeError, AccountReconciliationError, ValueError) as exc:
        LOG.error("Account inventory inconclusive (no order attempted): %s", exc)
        return 1
