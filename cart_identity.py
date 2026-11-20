"""Conservative identity checks on optional OVH Eco cart GET responses.

GET /order/cart/{cartId}/item and GET /order/cart/{cartId}/item/{itemId}/
configuration are observational only. Missing optional fields NEVER prove
the selected product, hardware or datacenter. The checker neither handles
credentials nor knows how to create/submit a checkout.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from ksle_monitor import ALLOWED_DATACENTERS, PLAN_CODE, REQUIRED_MEMORY, STORAGE_PRIORITIES

MAX_ROWS = 24


@dataclass(frozen=True)
class CartIdentityEvidence:
    status: str
    reason: str

    @property
    def verified(self) -> bool:
        return self.status == "VERIFIED"

    def summary(self) -> str:
        # Only fixed, operator-safe categories; no cart ID, URLs, raw item
        # descriptions, account identifiers or arbitrary API values.
        return (
            f"CART IDENTITY {self.status}: {self.reason}. "
            "Not proof of final renewal invoice, commitment-free terms or "
            "permission to checkout. "
        )


def unavailable() -> CartIdentityEvidence:
    return CartIdentityEvidence("UNVERIFIED", "optional read-only cart evidence unavailable")


def _bad(reason: str) -> CartIdentityEvidence:
    return CartIdentityEvidence("CONFLICT", reason)


def inspect_cart_identity(*, fqn: str, datacenter: str, base_item_id: int,
                          addons: list[str], configurations: dict[str, str],
                          cart_items: Any, cart_configurations: Any) -> CartIdentityEvidence:
    """Verify exact plan, RAM, disks, DC and one-month/default cart structure.

    A verified result is strictly structural and cannot serve as a binding
    financial/contract offer. All mismatches are reported using safe labels.
    """
    if (type(base_item_id) is not int or base_item_id <= 0
            or fqn not in (
                f"{PLAN_CODE}.{REQUIRED_MEMORY}.{disk}" for disk in STORAGE_PRIORITIES
            ) or datacenter not in ALLOWED_DATACENTERS
            or not isinstance(addons, list)
            or any(not isinstance(addon, str) for addon in addons)
            or not isinstance(configurations, dict)):
        return _bad("unexpected requested plan or hardware")
    storage = fqn.split(".")[2]
    expected_addons = {
        f"{REQUIRED_MEMORY}-{PLAN_CODE}", f"{storage}-{PLAN_CODE}",
    }
    if (not expected_addons.issubset(set(addons))
            or len(addons) != len(set(addons))
            or configurations.get("dedicated_datacenter") != datacenter
            or configurations.get("region") != ("canada" if datacenter == "bhs" else "europe")
            or configurations.get("dedicated_os") != "none_64.en"):
        return _bad("requested mandatory hardware or location inconsistent")

    return _inspect_readback(PLAN_CODE, base_item_id, addons, configurations,
                             cart_items, cart_configurations)


def inspect_manual_cart_identity(*, plan_code: str, fqn: str, datacenter: str,
                                 base_item_id: int, addons: list[str],
                                 configurations: dict[str, str],
                                 cart_items: Any,
                                 cart_configurations: Any) -> CartIdentityEvidence:
    """Only for manual diagnostics of other models; NEVER a purchase proof.

    The KS-LE-6 purchasing guard intentionally continues using the stricter
    inspect_cart_identity() with its exact 128-GB and disk allowlist.
    """
    if (not isinstance(plan_code, str)
            or re.fullmatch(r"27skle0[1236]-v1", plan_code) is None
            or not isinstance(fqn, str) or len(fqn) > 250
            or len(fqn.split(".")) != 3
            or fqn.split(".")[0] != plan_code
            or not re.fullmatch(r"ram-[a-z0-9-]+", fqn.split(".")[1])
            or not re.fullmatch(r"[a-z0-9-]+", fqn.split(".")[2])
            or datacenter not in ALLOWED_DATACENTERS
            or type(base_item_id) is not int or base_item_id <= 0
            or not isinstance(addons, list)
            or any(not isinstance(addon, str) for addon in addons)
            or len(addons) != len(set(addons))
            or not isinstance(configurations, dict)):
        return _bad("unexpected diagnostic plan, hardware or location")
    _, memory, storage = fqn.split(".")
    if (not {f"{memory}-{plan_code}", f"{storage}-{plan_code}"}.issubset(set(addons))
            or configurations.get("dedicated_datacenter") != datacenter
            or configurations.get("region") != ("canada" if datacenter == "bhs" else "europe")
            or configurations.get("dedicated_os") != "none_64.en"):
        return _bad("requested diagnostic hardware or location inconsistent")
    return _inspect_readback(plan_code, base_item_id, addons, configurations,
                             cart_items, cart_configurations)


def _inspect_readback(plan_code: str, base_item_id: int,
                      addons: list[str], configurations: dict[str, str],
                      cart_items: Any, cart_configurations: Any) -> CartIdentityEvidence:
    """Common strict GET-only item and configuration reconciliation."""

    if (not isinstance(cart_items, list) or not isinstance(cart_configurations, list)
            or not 0 < len(cart_items) <= MAX_ROWS
            or not 0 < len(cart_configurations) <= MAX_ROWS):
        return unavailable()

    expected = {plan_code, *addons}
    seen: set[str] = set()
    ids: set[int] = set()
    incomplete = False
    for item in cart_items:
        if not isinstance(item, dict):
            return unavailable()
        code = item.get("planCode")
        if code is None:
            incomplete = True
            continue
        if not isinstance(code, str) or code not in expected or code in seen:
            return _bad("unexpected or duplicated item plan")
        seen.add(code)
        item_id = item.get("itemId")
        if item_id is None:
            incomplete = True
        elif type(item_id) is not int or item_id <= 0 or item_id in ids:
            return _bad("invalid or repeated item identifier")
        else:
            ids.add(item_id)
            if code == plan_code and item_id != base_item_id:
                return _bad("base server identifier does not match")
        for field, value in (("duration", "P1M"), ("pricingMode", "default"), ("quantity", 1)):
            if field not in item:
                incomplete = True
            elif item[field] != value or (field == "quantity" and type(item[field]) is not int):
                return _bad("cart item term or quantity differs from selection")
    if len(seen) != len(expected):
        incomplete = True

    seen_config: set[str] = set()
    for row in cart_configurations:
        if not isinstance(row, dict):
            return unavailable()
        label = row.get("label")
        if label is None:
            incomplete = True
            continue
        if (not isinstance(label, str) or label not in configurations
                or label in seen_config or row.get("value") != configurations[label]):
            return _bad("cart location/OS configuration differs from selection")
        seen_config.add(label)
    if len(seen_config) != len(configurations):
        incomplete = True
    if incomplete:
        return CartIdentityEvidence("UNVERIFIED", "cart readback has incomplete item or configuration fields")
    return CartIdentityEvidence("VERIFIED", "exact cart item and configuration readback matches selection")
