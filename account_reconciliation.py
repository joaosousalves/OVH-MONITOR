"""Manual, GET-only OVHcloud account service inventory for KS-LE-6.

This does not reserve stock, mutate local purchase journals, or claim that
pending/unpaid orders are fully covered by the service list. All returned
service identifiers and account details stay out of logs and CLI output.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any, Mapping

from order_probe import OVHCartClient, OrderProbeError


LOG = logging.getLogger("ksle-account-inventory")
PLAN_ALIASES = frozenset(("27skle06-v1", "27skle06"))
MAX_IDS = 250  # Manual mode only: refuse unexpectedly large account scans.
DEDICATED_SERVICE_ROUTES = frozenset((
    "/dedicated/server", "/dedicated/server/{serviceName}",
))
SERVER_NAME = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\Z")
EXACT_COMMERCIAL_MODEL = re.compile(r"^KS-LE-6(?=$|[\s|(:;,-])", re.IGNORECASE)
# Never turn a slightly different KS-LE-6 spelling into a verified zero.
POSSIBLE_TARGET_MODEL = re.compile(r"(?:KS[-_\s]*LE[-_\s]*0?6|SKLE0?6)(?!\d)", re.IGNORECASE)


class AccountReconciliationError(Exception):
    """Unknown inventory state. Never interpret this as zero owned servers."""


@dataclass(frozen=True)
class AccountInventory:
    matching_services: int
    inspected_services: int
    max_servers: int
    inspected_hardware: int = 0

    @property
    def at_limit(self) -> bool:
        return self.matching_services >= self.max_servers

    def summary(self) -> str:
        return (
            f"ACCOUNT READ-ONLY: {self.matching_services} KS-LE-6 service(s) "
            f"found across {self.inspected_services} service(s) inspected; "
            f"{self.inspected_hardware} dedicated hardware lookup(s); "
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


def _dedicated_route(detail: dict) -> bool:
    route = detail.get("route")
    path = route.get("path") if isinstance(route, dict) else None
    if not isinstance(path, str) or not path.startswith("/"):
        raise AccountReconciliationError("Missing service route for service without plan code")
    if path in DEDICATED_SERVICE_ROUTES:
        return True
    if path.startswith("/dedicated/server"):
        raise AccountReconciliationError("Unrecognized dedicated server route")
    return False


def _hardware_is_ksle6(hardware: Any) -> bool:
    if not isinstance(hardware, dict):
        raise AccountReconciliationError("Missing dedicated server hardware response")
    description = hardware.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 300:
        raise AccountReconciliationError("Missing dedicated server commercial model")
    model = description.strip()
    if EXACT_COMMERCIAL_MODEL.match(model):
        return True
    if POSSIBLE_TARGET_MODEL.search(model):
        raise AccountReconciliationError("Ambiguous KS-LE-6 commercial model")
    return False


def _dedicated_service_name(detail: dict) -> str:
    route = detail["route"]
    variables = route.get("vars")
    if not isinstance(variables, list):
        raise AccountReconciliationError("Missing dedicated server routing variables")
    names = [v.get("value") for v in variables
             if isinstance(v, dict) and v.get("key") == "serviceName"]
    if len(names) > 1:
        raise AccountReconciliationError("Ambiguous dedicated server routing variables")
    resource = detail.get("resource")
    resource_name = resource.get("name") if isinstance(resource, dict) else None
    name = names[0] if names else resource_name
    if (not isinstance(name, str) or not 0 < len(name) <= 253
            or not SERVER_NAME.fullmatch(name)):
        raise AccountReconciliationError("Invalid dedicated server name")
    return name


def analyze_services(ids: Any, details: list[Any], max_servers: int = 2,
                     hardware_by_service_id: Mapping[int, Any] | None = None) -> AccountInventory:
    valid_ids = validate_service_ids(ids)
    if type(max_servers) is not int or max_servers not in (1, 2):
        raise AccountReconciliationError("Invalid purchase quota")
    if not isinstance(details, list) or len(details) != len(valid_ids):
        raise AccountReconciliationError("Incomplete account inventory: no zero-count assumption")
    total = 0
    inspected_hardware = 0
    for service_id, detail in zip(valid_ids, details):
        if not isinstance(detail, dict) or not isinstance(detail.get("plan"), dict):
            raise AccountReconciliationError("Incomplete service plan metadata")
        code = detail["plan"].get("code")
        if code is not None and not isinstance(code, str):
            raise AccountReconciliationError("Malformed service plan code")
        if isinstance(code, str) and not code.strip():
            code = None
        if code is None:
            # OVH can return null plan codes for every service in the account.
            # Never infer zero for a dedicated server without its hardware.
            if not _dedicated_route(detail):
                continue
            if hardware_by_service_id is None or service_id not in hardware_by_service_id:
                raise AccountReconciliationError("Missing dedicated server hardware classification")
            is_target = _hardware_is_ksle6(hardware_by_service_id[service_id])
            inspected_hardware += 1
        else:
            # All service IDs must be inspected; uncertain KS-LE-6 spellings
            # must not be silently excluded from the owner-wide quota.
            normalized = code.lower().replace("-", "")
            if code.lower() in PLAN_ALIASES:
                is_target = True
            elif any(token in normalized for token in ("skle06", "ksle06", "ksle6")):
                raise AccountReconciliationError("Ambiguous KS-LE-6 plan code; manual reconciliation required")
            else:
                is_target = False
        if is_target:
            quantity = detail.get("quantity")
            if type(quantity) is not int or not 0 < quantity <= 100:
                raise AccountReconciliationError("Invalid KS-LE-6 service quantity")
            total += quantity
    return AccountInventory(total, len(valid_ids), max_servers, inspected_hardware)


def reconcile_account(client: OVHCartClient, max_servers: int = 2) -> AccountInventory:
    """GET-only account scan; fetch missing-plan dedicated hardware if needed."""
    ids = validate_service_ids(client.request("GET", "/service"))
    if type(max_servers) is not int or max_servers not in (1, 2):
        raise AccountReconciliationError("Invalid purchase quota")
    try:
        details = [client.request("GET", f"/service/{service_id}") for service_id in ids]
    except OrderProbeError as exc:
        raise AccountReconciliationError("Cannot retrieve all account service details") from exc
    hardware_by_id: dict[int, Any] = {}
    seen_names: set[str] = set()
    for service_id, detail in zip(ids, details):
        if not isinstance(detail, dict) or not isinstance(detail.get("plan"), dict):
            raise AccountReconciliationError("Incomplete service plan metadata")
        raw_code = detail["plan"].get("code")
        if raw_code is not None and not (isinstance(raw_code, str) and not raw_code.strip()):
            continue
        if not _dedicated_route(detail):
            continue
        name = _dedicated_service_name(detail)
        if name in seen_names:
            raise AccountReconciliationError("Duplicate dedicated server identity")
        seen_names.add(name)
        try:
            hardware_by_id[service_id] = client.request(
                "GET", f"/dedicated/server/{name}/specifications/hardware"
            )
        except OrderProbeError as exc:
            # Do not log the service hostname, even on HTTP 403.
            raise AccountReconciliationError(
                "Cannot retrieve dedicated server hardware; check GET hardware API permission"
            ) from exc
    return analyze_services(ids, details, max_servers, hardware_by_id)


def run_account_reconciliation() -> int:
    try:
        from purchase_guard import PurchasePolicy, make_api_client
        policy = PurchasePolicy.from_env()
        print(reconcile_account(make_api_client(), policy.max_servers).summary())
        return 0
    except (OrderProbeError, AccountReconciliationError, ValueError) as exc:
        LOG.error("Account inventory inconclusive (no order attempted): %s", exc)
        return 1
