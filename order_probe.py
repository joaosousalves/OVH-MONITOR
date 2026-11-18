"""Non-purchasing OVHcloud Eco cart probe for the KS-LE-6.

Only an explicitly allowlisted set of cart endpoints can be called. In
particular, POST /checkout, payment endpoints, and the OVH account's orders
are deliberately unsupported. A temporary test cart is deleted on exit.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://api.eu.ovhcloud.com/v1"
PLAN_CODE = "27skle06-v1"
PREFERRED_STORAGE_OPTION = "hybridsoftraid-2x4000sa-2x500nvme-27skle06-v1"
DATACENTERS = {"bhs", "fra", "gra", "lon", "rbx", "sbg", "waw"}
LOGGER = logging.getLogger("ksle-order-probe")

# There are intentionally NO checkout POST, order creation or payment routes.
SAFE_ROUTES = {
    "GET": [
        re.compile(r"^/order/catalog/public/eco$"),
        re.compile(r"^/order/cart/[A-Za-z0-9-]+/eco$"),
        re.compile(r"^/order/cart/[A-Za-z0-9-]+/checkout$"),
    ],
    "POST": [
        re.compile(r"^/order/cart$"),
        re.compile(r"^/order/cart/[A-Za-z0-9-]+/assign$"),
        re.compile(r"^/order/cart/[A-Za-z0-9-]+/eco$"),
        re.compile(r"^/order/cart/[A-Za-z0-9-]+/eco/options$"),
        re.compile(r"^/order/cart/[A-Za-z0-9-]+/item/[0-9]+/configuration$"),
    ],
    "DELETE": [
        re.compile(r"^/order/cart/[A-Za-z0-9-]+$"),
    ],
}


class OrderProbeError(Exception):
    """An unsafe operation, unexpected response or OVHcloud API failure."""


class OVHCartClient:
    def __init__(self, bearer_token: str, timeout: int = 20):
        if not bearer_token or bearer_token.isspace():
            raise OrderProbeError("OVH_TOKEN must be configured before testing a cart")
        self.token = bearer_token
        self.timeout = timeout

    def request(self, method: str, path: str, payload: dict | None = None,
                query: dict | None = None) -> Any:
        method = method.upper()
        if not any(rule.fullmatch(path) for rule in SAFE_ROUTES.get(method, [])):
            raise OrderProbeError(f"Blocked unsupported operation: {method} {path}")
        if method == "GET" and payload is not None:
            raise OrderProbeError("GET must not include a body")
        url = BASE_URL + path
        if query:
            url += "?" + urlencode(query)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json", "User-Agent": "ksle-monitor/1.1"}
        if path != "/order/catalog/public/eco":
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, method=method, headers=headers)
        # The public PT Eco catalog is currently ~13 MB. Cart replies are small.
        limit = 25_000_000 if path == "/order/catalog/public/eco" else 2_000_000
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise OrderProbeError("Unexpectedly large API response")
        except HTTPError as exc:
            # Do not log the authorization header or token.
            error_text = exc.read(450).decode("utf-8", errors="replace")
            raise OrderProbeError(f"OVHcloud API {exc.code} on {method} {path}: {error_text}") from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise OrderProbeError(f"OVHcloud connection error on {method} {path}: {exc}") from exc
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise OrderProbeError(f"Non-JSON API response for {method} {path}") from exc


def catalog_selection(catalog: Any, storage_option: str, datacenter: str) -> tuple[dict, list[str]]:
    if not isinstance(catalog, dict):
        raise OrderProbeError("Public Eco catalog has an unexpected format")
    plans = catalog.get("plans")
    if not isinstance(plans, list):
        raise OrderProbeError("Eco catalog is missing plans")
    plan = next((p for p in plans if isinstance(p, dict) and p.get("planCode") == PLAN_CODE), None)
    if plan is None:
        raise OrderProbeError(f"Plan {PLAN_CODE} is not in OVHcloud's PT catalog")
    allowed_config = {c.get("name"): c.get("values") for c in plan.get("configurations", []) if isinstance(c, dict)}
    if datacenter not in allowed_config.get("dedicated_datacenter", []):
        raise OrderProbeError(f"Datacenter {datacenter} is not offered for this plan")
    region = "canada" if datacenter == "bhs" else "europe"
    if region not in allowed_config.get("region", []):
        raise OrderProbeError(f"Region {region} is not offered for this plan")
    if "none_64.en" not in allowed_config.get("dedicated_os", []):
        raise OrderProbeError("Expected OS option none_64.en is unavailable")

    chosen_addons: list[str] = []
    seen_storage = False
    for family in plan.get("addonFamilies", []):
        if not isinstance(family, dict) or not family.get("mandatory"):
            continue
        addons = family.get("addons")
        if not isinstance(addons, list) or not addons:
            raise OrderProbeError("Invalid mandatory addon family")
        if family.get("name") == "storage":
            seen_storage = True
            if storage_option not in addons:
                raise OrderProbeError(f"Storage option {storage_option} is not available in the catalog")
            chosen_addons.append(storage_option)
        else:
            default = family.get("default")
            if default not in addons:
                raise OrderProbeError(f"No valid default for {family.get('name')} addon")
            chosen_addons.append(default)
    if not seen_storage:
        raise OrderProbeError("Storage addon family is missing from catalog")
    return {"dedicated_datacenter": datacenter, "region": region, "dedicated_os": "none_64.en"}, chosen_addons


def find_monthly_offer(offers: Any) -> dict:
    if not isinstance(offers, list):
        raise OrderProbeError("Eco order offers are not a list")
    for offer in offers:
        if (isinstance(offer, dict) and offer.get("planCode") == PLAN_CODE
                and offer.get("duration") == "P1M" and offer.get("pricingMode") == "default"):
            return offer
    raise OrderProbeError(f"No orderable monthly offer found for {PLAN_CODE} in your OVHcloud account")


def extract_quote(quote_data: Any) -> str:
    if not isinstance(quote_data, dict):
        raise OrderProbeError("Checkout quotation has an unexpected format")
    prices = quote_data.get("prices")
    if not isinstance(prices, dict) or not isinstance(prices.get("withTax"), dict):
        raise OrderProbeError("Checkout quotation contains no total with tax")
    price = prices["withTax"]
    display = price.get("text")
    if not isinstance(display, str) or not display:
        raise OrderProbeError("Checkout quotation has no displayable price")
    return display


def test_order(client: OVHCartClient, datacenter: str = "bhs",
               storage_option: str = PREFERRED_STORAGE_OPTION, subsidiary: str = "PT") -> str:
    """Build and quote a temporary cart; NEVER submit checkout or payment."""
    datacenter = datacenter.lower()
    if datacenter not in DATACENTERS:
        raise OrderProbeError(f"Unsupported datacenter: {datacenter}")
    if subsidiary != "PT":
        raise OrderProbeError("This monitor's test-order flow is restricted to OVH Portugal (PT)")

    catalog = client.request("GET", "/order/catalog/public/eco", query={"ovhSubsidiary": subsidiary})
    configurations, addons = catalog_selection(catalog, storage_option, datacenter)

    LOGGER.info("Starting temporary test cart for %s in %s (NO ORDER / NO PAYMENT)", PLAN_CODE, datacenter.upper())
    cart_id: str | None = None
    try:
        cart_response = client.request("POST", "/order/cart", {"ovhSubsidiary": subsidiary})
        if not isinstance(cart_response, dict) or not isinstance(cart_response.get("cartId"), str):
            raise OrderProbeError("Cart creation returned no cartId")
        cart_id = cart_response["cartId"]
        cart_path = "/order/cart/" + quote(cart_id, safe="")
        client.request("POST", cart_path + "/assign", {})
        offers = client.request("GET", cart_path + "/eco")
        offer = find_monthly_offer(offers)
        order_item = client.request("POST", cart_path + "/eco", {
            "planCode": offer["planCode"],
            "duration": offer["duration"],
            "pricingMode": offer["pricingMode"],
            "quantity": 1,
        })
        item_id = order_item.get("itemId") if isinstance(order_item, dict) else None
        if not isinstance(item_id, int) or item_id <= 0:
            raise OrderProbeError("Adding server to cart returned no valid itemId")

        for addon in addons:
            client.request("POST", cart_path + "/eco/options", {
                "itemId": item_id,
                "planCode": addon,
                "duration": offer["duration"],
                "pricingMode": offer["pricingMode"],
                "quantity": 1,
            })
        for label, value in configurations.items():
            client.request("POST", cart_path + f"/item/{item_id}/configuration", {
                "label": label,
                "value": value,
            })
        quote_data = client.request("GET", cart_path + "/checkout")
        price = extract_quote(quote_data)
        return f"Carrinho de teste validado: {PLAN_CODE}, {datacenter.upper()}, {storage_option}; total com IVA: {price}. Nenhuma encomenda criada."
    finally:
        if cart_id is not None:
            try:
                client.request("DELETE", "/order/cart/" + quote(cart_id, safe=""))
                LOGGER.info("Temporary test cart deleted")
            except OrderProbeError as exc:
                LOGGER.warning("Unable to delete temporary test cart: %s", exc)


def run_test_order(datacenter: str, storage: str) -> int:
    token = os.getenv("OVH_TOKEN", "").strip()
    if not token:
        LOGGER.error("OVH_TOKEN missing; configure it locally in a protected environment file")
        return 1
    storage_code = PREFERRED_STORAGE_OPTION if storage == "preferred" else storage
    client = OVHCartClient(token)
    try:
        result = test_order(client, datacenter=datacenter, storage_option=storage_code)
        print(result)
        return 0
    except OrderProbeError as exc:
        LOGGER.error("Test-order failed (no purchase was made): %s", exc)
        return 1
