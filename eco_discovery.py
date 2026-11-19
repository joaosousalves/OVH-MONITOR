"""Read-only inventory discovery for six OVHcloud PT Kimsufi LE plans.

This never authenticates, creates carts, updates monitoring state or writes a
purchase journal. OVHcloud availability codes are lead-time indications, NOT
physical stock quantities and NOT a confirmed checkout.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from ksle_monitor import APITransientError, MonitorError, Stock, retrieve_stock

LOGGER = logging.getLogger("ksle-catalogue-discovery")
PLANS = {
    "KS-LE-1": "27skle01-v1",
    "KS-LE-2": "27skle02-v1",
    "KS-LE-3": "27skle03-v1",
    "KS-LE-4": "27skle04-v1",
    "KS-LE-5": "27skle05-v1",
    "KS-LE-6": "27skle06-v1",
}
PRODUCT_BASE = "https://eco.ovhcloud.com/pt/kimsufi/"
MAX_ROWS_PER_PLAN = 150


def hardware_parts(stock: Stock) -> tuple[str, str]:
    """Show unmodified hardware codes, rather than guessing disk capacities."""
    parts = stock.fqn.split(".")
    if len(parts) != 3 or not parts[0].startswith("27skle"):
        raise MonitorError("Unexpected stock FQN for the selected KS-LE plan")
    return parts[1], parts[2]


def render_options(fetch: Callable[..., list[Stock]] = retrieve_stock,
                   sleep: Callable[[float], None] = time.sleep,
                   pause_seconds: float = 1.0) -> tuple[str, bool]:
    """Return complete read-only report and whether every plan was checked.

    An API error or an empty response is marked CHECK FAILED, not sold out.
    """
    lines = [
        "OVHcloud KS-LE catalogue availability (PT / EU+CA)",
        "Status such as 1H-high = delivery estimate, not number of units.",
        "Listings are NOT reserved and prices require a separate cart quotation.",
    ]
    all_checked = True
    for index, (label, plan_code) in enumerate(PLANS.items()):
        if index and pause_seconds > 0:
            sleep(pause_seconds)  # avoid bursting six parallel stock API requests
        url = PRODUCT_BASE + label.lower() + "/"
        try:
            rows = fetch(plan_code=plan_code)
            if not rows or len(rows) > MAX_ROWS_PER_PLAN:
                raise MonitorError("Empty, incomplete or unexpectedly large stock feed")
            if any(stock.fqn.split(".", 1)[0] != plan_code for stock in rows):
                raise MonitorError("Stock feed returned a mismatched plan")
            available = sorted((stock for stock in rows if stock.available),
                               key=lambda stock: (stock.datacenter, stock.fqn))
            lines.append(
                f"\n{label} | {plan_code} | {len(available)} available configuration/datacenter combinations"
            )
            lines.append("Product: " + url)
            if not available:
                lines.append("No currently orderable configuration reported.")
            for stock in available:
                memory, storage = hardware_parts(stock)
                lines.append(
                    f"  {stock.datacenter.upper():<4} {stock.status:<12} "
                    f"RAM={memory} | disks={storage} | FQN={stock.fqn}"
                )
        except APITransientError as exc:
            # Honor OVH API backoff: never continue making requests after
            # a rate-limit or temporary upstream outage.
            all_checked = False
            lines.append(f"\n{label} | {plan_code} | CHECK FAILED: {exc}")
            lines.append("Stopping further requests to respect API cooldown.")
            lines.append("Remaining plans NOT CHECKED; availability unknown.")
            break
        except (MonitorError, OSError, ValueError) as exc:
            all_checked = False
            lines.append(f"\n{label} | {plan_code} | CHECK FAILED: {exc}")
            lines.append("Availability unknown; do not assume out of stock.")
    return "\n".join(lines), all_checked


def run_list_eco_options() -> int:
    output, checked = render_options()
    print(output)
    if not checked:
        LOGGER.warning("One or more stock requests failed; results are incomplete")
        return 1
    return 0
