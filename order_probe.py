"""Non-purchasing OVHcloud Eco cart probe for the KS-LE-6.

Only an explicitly allowlisted set of cart endpoints can be called. In
particular, POST /checkout, payment endpoints, and the OVH account's orders
are deliberately unsupported. A temporary test cart is deleted on exit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://api.eu.ovhcloud.com/v1"
TIME_URL = "https://api.eu.ovhcloud.com/1.0/auth/time"
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
    def __init__(self, bearer_token: str = "", timeout: int = 20, *,
                 application_key: str = "", application_secret: str = "",
                 consumer_key: str = ""):
        """Use signed OVH AK/AS/CK credentials or an existing Bearer token."""
        credentials = (application_key, application_secret, consumer_key)
        if any(credentials):
            if not all(credentials) or bearer_token:
                raise OrderProbeError("Supply all three OVH API keys, without OVH_TOKEN")
            self.signed = True
        elif bearer_token and not bearer_token.isspace():
            self.signed = False
        else:
            raise OrderProbeError(
                "Set OVH_APPLICATION_KEY, OVH_APPLICATION_SECRET, OVH_CONSUMER_KEY "
                "or a valid OVH_TOKEN"
            )
        self.token = bearer_token
        self.application_key = application_key
        self.application_secret = application_secret
        self.consumer_key = consumer_key
        self.timeout = timeout
        self._clock_offset: float | None = None

    def _timestamp(self) -> str:
        """Use OVH server time, to prevent signed requests failing due to clock drift."""
        if self._clock_offset is None:
            try:
                req = Request(TIME_URL, headers={"Accept": "application/json"})
                before = time.time()
                with urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read(64).decode("ascii").strip()
                after = time.time()
                remote_time = int(raw)
                self._clock_offset = remote_time - ((before + after) / 2)
            except (HTTPError, URLError, OSError, ValueError, UnicodeError) as exc:
                raise OrderProbeError("Could not obtain OVHcloud API signing timestamp") from exc
        return str(int(time.time() + self._clock_offset))

    def _signed_headers(self, method: str, url: str, body: bytes | None) -> dict[str, str]:
        timestamp = self._timestamp()
        signing_text = "+".join((
            self.application_secret, self.consumer_key, method, url,
            body.decode("utf-8") if body is not None else "", timestamp,
        ))
        signature = "$1$" + hashlib.sha1(signing_text.encode("utf-8")).hexdigest()
        return {
            "X-Ovh-Application": self.application_key,
            "X-Ovh-Consumer": self.consumer_key,
            "X-Ovh-Timestamp": timestamp,
            "X-Ovh-Signature": signature,
        }

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
            if self.signed:
                headers.update(self._signed_headers(method, url, body))
            else:
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


def matching_offer_schema(row: dict) -> str:
    """Restricted shape-only diagnostic; no raw values, identities or credentials."""
    # API response formats can vary. Show field names and container shapes only,
    # restricted to a single matching plan and at most two nesting levels.
    def keys(value: dict) -> str:
        safe = sorted(k for k in value if isinstance(k, str)
                      and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", k))
        shown = ", ".join(safe[:30])
        return shown + (", ..." if len(safe) > 30 else "")

    details = []
    for name, value in list(row.items())[:30]:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", name):
            continue
        if isinstance(value, dict):
            details.append(f"{name}=object({keys(value)})")
            for subname, subvalue in list(value.items())[:12]:
                if (isinstance(subname, str)
                        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", subname)
                        and isinstance(subvalue, list) and subvalue
                        and isinstance(subvalue[0], dict)):
                    details.append(f"{name}.{subname}[0]=object({keys(subvalue[0])})")
        elif isinstance(value, list):
            first = value[0] if value else None
            if isinstance(first, dict):
                details.append(f"{name}=list({len(value)}), first=object({keys(first)})")
            else:
                details.append(f"{name}=list({len(value)})")
    return "top-level=[" + keys(row) + "]; nested=[" + "; ".join(details[:20]) + "]"


def offer_diagnostic(offers: Any) -> str:
    """Log only plan and billing fields, never the raw account/cart response."""
    if not isinstance(offers, list):
        return f"Unexpected Eco response type: {type(offers).__name__}"
    rows = [r for r in offers if isinstance(r, dict)]
    matching = [r for r in rows if r.get("planCode") == PLAN_CODE]
    sample = matching if matching else rows
    def safe_value(value: Any) -> str:
        # The response is untrusted; prevent huge/structured values in logs.
        return str(value)[:70] if isinstance(value, (str, int, float, bool)) else "missing"
    shown = [
        "(" + ", ".join(
            f"{key}={safe_value(row.get(key))}"
            for key in ("planCode", "duration", "pricingMode")
        ) + ")"
        for row in sample[:8]
    ]
    return (
        f"GET /order/cart/{{cartId}}/eco: {len(offers)} offers returned, "
        f"{len(matching)} match planCode={PLAN_CODE}. "
        f"Offer samples: {', '.join(shown) if shown else 'none'}"
        + (f". Matching offer fields: {matching_offer_schema(matching[0])}" if matching else "")
    )


def find_monthly_offer(offers: Any) -> dict:
    if not isinstance(offers, list):
        raise OrderProbeError(offer_diagnostic(offers))
    for offer in offers:
        if (isinstance(offer, dict) and offer.get("planCode") == PLAN_CODE
                and offer.get("duration") == "P1M" and offer.get("pricingMode") == "default"):
            return offer
    raise OrderProbeError(
        "No exactly matching monthly KS-LE-6 offer. "
        + offer_diagnostic(offers)
        + ". This may reflect unavailable stock or different offer terms."
    )


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
    ak = os.getenv("OVH_APPLICATION_KEY", "").strip()
    secret = os.getenv("OVH_APPLICATION_SECRET", "").strip()
    ck = os.getenv("OVH_CONSUMER_KEY", "").strip()
    storage_code = PREFERRED_STORAGE_OPTION if storage == "preferred" else storage
    try:
        client = OVHCartClient(
            token, application_key=ak, application_secret=secret, consumer_key=ck
        )
        result = test_order(client, datacenter=datacenter, storage_option=storage_code)
        print(result)
        return 0
    except OrderProbeError as exc:
        LOGGER.error("Test-order failed (no purchase was made): %s", exc)
        return 1
