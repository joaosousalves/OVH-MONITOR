"""Manual, read-only reconciliation of KS-LE-6 open purchase orders.

No order is created, paid, cancelled or retried. Historical delivered orders
are not counted again as open orders; the service inventory is queried
separately. If both an open order and a matching service exist, their identity
cannot yet be linked reliably, so the combined quota remains inconclusive.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
import logging
import re
from typing import Any

from account_reconciliation import (
    AccountInventory, AccountReconciliationError, PLAN_ALIASES, reconcile_account,
)
from order_probe import OVHCartClient, OrderProbeError


LOG = logging.getLogger("ksle-pending-orders")
MAX_ORDERS = 250
MAX_ORDER_DETAILS = 100
MAX_ORDER_ID = 10**15
TERMINAL_STATUSES = frozenset(("cancelled", "delivered"))
OPEN_STATUSES = frozenset((
    "notPaid", "checking", "delivering", "documentsRequested", "cancelling",
))
QUANTITY_PATTERN = re.compile(r"[1-9][0-9]{0,2}\Z")


class PendingOrdersError(Exception):
    """A pending order count cannot be safely proved to be complete."""


@dataclass(frozen=True)
class PendingOrderInventory:
    services: AccountInventory
    inspected_orders: int
    open_orders: int
    pending_servers: int
    expired_unpaid_orders: int = 0

    @property
    def overlap_unverified(self) -> bool:
        return self.services.matching_services > 0 and self.pending_servers > 0

    @property
    def lower_bound(self) -> int:
        return max(self.services.matching_services, self.pending_servers)

    @property
    def upper_bound(self) -> int:
        return self.services.matching_services + self.pending_servers

    def summary(self) -> str:
        base = (
            f"PENDING ORDERS READ-ONLY: {self.services.matching_services} "
            f"KS-LE-6 service(s), {self.pending_servers} open KS-LE-6 order item(s); "
            f"{self.inspected_orders} order(s) checked, {self.open_orders} open; "
            f"{self.expired_unpaid_orders} expired unpaid order(s) excluded; "
            f"cap {self.services.max_servers}. "
        )
        if self.overlap_unverified:
            quota = (
                "INCONCLUSIVE OVERLAP: cannot prove whether an open order "
                "also appears as a provisioned service; possible total "
                f"{self.lower_bound}..{self.upper_bound}. Manual review required. "
            )
        else:
            quota = (
                f"Verified service + open-order count {self.upper_bound}/"
                f"{self.services.max_servers}. "
                + ("LIMIT REACHED. " if self.upper_bound >= self.services.max_servers
                   else "Below current quota. ")
            )
        return (
            base + quota
            + "Delivered orders are excluded to avoid counting provisioned servers "
            "twice; very recent deliveries may not yet appear in /service. "
            "This is not proof of a lifetime purchase count. "
            "Checkout/payment remain disabled."
        )


def validate_order_ids(ids: Any) -> list[int]:
    if not isinstance(ids, list) or len(ids) > MAX_ORDERS:
        raise PendingOrdersError("Order list missing or exceeds bounded scan")
    if any(type(i) is not int or not 0 < i < MAX_ORDER_ID for i in ids):
        raise PendingOrdersError("Invalid order identifier in inventory")
    if len(set(ids)) != len(ids):
        raise PendingOrdersError("Duplicate order identifiers in inventory")
    return ids


def _order_status(raw: Any) -> str:
    status = raw.get("status") if isinstance(raw, dict) else raw
    if not isinstance(status, str):
        raise PendingOrdersError("Missing purchase order status")
    if status in TERMINAL_STATUSES or status in OPEN_STATUSES:
        return status
    # Includes 'unknown', and any provider state introduced in the future.
    raise PendingOrdersError("Unrecognized purchase order status")


def _unpaid_order_expired(raw: Any, order_id: int, today_utc: date) -> bool:
    """Exclude a notPaid order only when its expiry predates the UTC day.

    OVH documents unpaid purchase orders as no longer purchasable after their
    expiration date. Unknown timestamps must fail closed, not imply expiry.
    """
    if not isinstance(raw, dict):
        raise PendingOrdersError("Missing unpaid order metadata")
    returned_id = raw.get("orderId")
    if returned_id is not None and returned_id != order_id:
        raise PendingOrdersError("Mismatched unpaid order identifier")
    expiration = raw.get("expirationDate")
    if not isinstance(expiration, str) or not expiration.strip():
        raise PendingOrdersError("Missing unpaid order expiration date")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", expiration):
            expiration_day = date.fromisoformat(expiration)
        else:
            stamp = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("Unspecified expiration timezone")
            expiration_day = stamp.astimezone(timezone.utc).date()
    except ValueError as exc:
        raise PendingOrdersError("Invalid or timezone-ambiguous unpaid order expiration date") from exc
    return expiration_day < today_utc


def _order_detail_ids(raw: Any) -> list[int]:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_ORDER_DETAILS:
        raise PendingOrdersError("Missing or oversized pending order details")
    if any(type(i) is not int or not 0 < i < MAX_ORDER_ID for i in raw):
        raise PendingOrdersError("Invalid pending order detail identifier")
    if len(set(raw)) != len(raw):
        raise PendingOrdersError("Duplicate pending order detail identifier")
    return raw


def _quantity(detail: dict) -> int:
    raw = detail.get("quantity")
    if isinstance(raw, str) and QUANTITY_PATTERN.fullmatch(raw):
        value = int(raw)
    elif type(raw) is int and 0 < raw <= 100:
        value = raw
    else:
        raise PendingOrdersError("Invalid KS-LE-6 ordered quantity")
    if value > 100:
        raise PendingOrdersError("Unexpected KS-LE-6 ordered quantity")
    return value


def _is_target_plan(extension: Any) -> bool:
    if not isinstance(extension, dict):
        raise PendingOrdersError("Missing pending order plan extension")
    order = extension.get("order")
    plan = order.get("plan") if isinstance(order, dict) else None
    code = plan.get("code") if isinstance(plan, dict) else None
    if not isinstance(code, str) or not code.strip():
        raise PendingOrdersError("Missing pending order plan code")
    lower = code.strip().lower()
    if lower in PLAN_ALIASES:
        return True
    normalized = re.sub(r"[^a-z0-9]", "", lower)
    if any(token in normalized for token in ("skle06", "ksle06", "ksle6")):
        raise PendingOrdersError("Ambiguous KS-LE-6 plan code in pending order")
    return False


def _group_key(detail: dict) -> tuple[str, str | int]:
    domain = detail.get("domain")
    if isinstance(domain, str) and domain.strip():
        domain = domain.strip()
        # OVH order lines can use *001 and *001.001 for one product.
        if re.fullmatch(r"\*[0-9]+(?:\.[0-9]+)*", domain):
            domain = domain.split(".", 1)[0]
        return ("domain", domain)
    cart_item = detail.get("cartItemID")
    if type(cart_item) is int and 0 < cart_item < MAX_ORDER_ID:
        return ("cart", cart_item)
    raise PendingOrdersError("Cannot identify distinct items within pending order")


def count_order_items(rows: list[tuple[Any, Any]]) -> int:
    """Count unique purchase items, not billing/installation detail lines."""
    if not isinstance(rows, list) or not rows:
        raise PendingOrdersError("Missing pending order lines")
    groups: dict[tuple[str, str | int], tuple[int, int | None]] = {}
    for detail, extension in rows:
        if not isinstance(detail, dict):
            raise PendingOrdersError("Malformed pending order detail")
        if not _is_target_plan(extension):
            continue
        key = _group_key(detail)
        quantity = _quantity(detail)
        cart_item = detail.get("cartItemID")
        if type(cart_item) is not int or not 0 < cart_item < MAX_ORDER_ID:
            cart_item = None
        previous = groups.get(key)
        if previous is not None:
            old_quantity, old_cart_item = previous
            if (old_quantity != quantity or
                    (old_cart_item is not None and cart_item is not None
                     and old_cart_item != cart_item)):
                raise PendingOrdersError("Conflicting duplicate pending order line")
        else:
            groups[key] = (quantity, cart_item)
    return sum(quantity for quantity, _ in groups.values())


def _get(client: OVHCartClient, path: str, label: str) -> Any:
    try:
        return client.request("GET", path)
    except OrderProbeError as exc:
        # The original API exception may contain a private order identifier.
        raise PendingOrdersError(f"Unable to read {label}; verify GET API rights") from exc


def reconcile_pending_orders(client: OVHCartClient, max_servers: int = 2,
                             *, service_check=reconcile_account,
                             as_of_utc_date: date | None = None,
                             read_workers: int = 1) -> PendingOrderInventory:
    """Exhaustive bounded GET scan; no POST, temporary carts, email or writes."""
    if as_of_utc_date is not None and type(as_of_utc_date) is not date:
        raise PendingOrdersError("Invalid inventory reference date")
    if type(read_workers) is not int or not 1 <= read_workers <= 3:
        raise PendingOrdersError("Invalid read-only inventory concurrency")
    today_utc = as_of_utc_date or datetime.now(timezone.utc).date()
    service_inventory = service_check(client, max_servers)
    ids = validate_order_ids(_get(client, "/me/order", "account order list"))
    pending = 0
    open_orders = 0
    expired_unpaid_orders = 0
    def status_for(order_id):
        return _order_status(_get(client, f"/me/order/{order_id}/status", "order status"))

    def fresh_statuses():
        if read_workers == 1:
            for order_id in ids:
                yield order_id, status_for(order_id)
            return
        # At most three independent GETs in flight. Do not queue the entire
        # order history: a failed request stops subsequent batches.
        with ThreadPoolExecutor(max_workers=read_workers) as pool:
            for start in range(0, len(ids), read_workers):
                batch = ids[start:start + read_workers]
                statuses = list(pool.map(status_for, batch))
                yield from zip(batch, statuses)

    for order_id, status in fresh_statuses():
        if status in TERMINAL_STATUSES:
            continue
        if status == "notPaid":
            metadata = _get(client, f"/me/order/{order_id}", "unpaid order expiration")
            if _unpaid_order_expired(metadata, order_id, today_utc):
                expired_unpaid_orders += 1
                continue
        open_orders += 1
        detail_ids = _order_detail_ids(_get(client, f"/me/order/{order_id}/details", "order details"))
        rows: list[tuple[Any, Any]] = []
        for detail_id in detail_ids:
            base = f"/me/order/{order_id}/details/{detail_id}"
            detail = _get(client, base, "order detail")
            if not isinstance(detail, dict):
                raise PendingOrdersError("Malformed pending order detail")
            returned_id = detail.get("orderDetailId")
            if returned_id is not None and returned_id != detail_id:
                raise PendingOrdersError("Mismatched pending order detail identifier")
            extension = _get(client, base + "/extension", "order plan extension")
            rows.append((detail, extension))
        pending += count_order_items(rows)
    return PendingOrderInventory(
        service_inventory, len(ids), open_orders, pending, expired_unpaid_orders,
    )


def run_pending_order_reconciliation() -> int:
    try:
        from purchase_guard import PurchasePolicy, make_api_client
        policy = PurchasePolicy.from_env()
        print(reconcile_pending_orders(make_api_client(), policy.max_servers).summary())
        return 0
    except (PendingOrdersError, AccountReconciliationError, OrderProbeError,
            ValueError, TypeError) as exc:
        LOG.error("Pending order inventory inconclusive (no order attempted): %s", exc)
        return 1
