"""Conservative, cached public OVH Eco monthly pricing (no cart, no purchase).

Return None unless the monthly base plan AND all mandatory addons have an
unambiguous public price. Values are catalog estimates *excluding VAT*, not
checkout totals. Uses only Python standard library.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import json
import logging
import os
import tempfile
import time
from urllib.request import Request, urlopen

PLAN = "27skle06-v1"
CATALOG = "https://api.eu.ovhcloud.com/v1/order/catalog/public/eco?ovhSubsidiary=PT"
TTL_SECONDS = 6 * 3600
RETRY_SECONDS = 3600  # Avoid hammering catalog on failure.
LOG = logging.getLogger("ksle-monitor")


def monthly_price(item: dict) -> Decimal | None:
    """Choose a single ordinary monthly EUR price denominated in 1e-8 units."""
    if not isinstance(item, dict):
        return None
    matches = [
        row for row in item.get("pricings", [])
        if isinstance(row, dict)
        and row.get("duration") == "P1M"
        and row.get("pricingMode") == "default"
        and row.get("phase", 0) in (0, None)
        and isinstance(row.get("price"), int)
        and not isinstance(row["price"], bool)
        and row["price"] >= 0
    ]
    # Ambiguous prices must not silently pick the wrong one.
    if len(matches) != 1:
        return None
    return Decimal(matches[0]["price"]) / Decimal(100000000)


def extract_prices(data: dict) -> dict[str, str]:
    """Price by *stock storage code*; complete mandatory addon prices required."""
    if not isinstance(data, dict):
        raise ValueError("Unexpected catalog JSON")
    locale = data.get("locale")
    if not isinstance(locale, dict) or locale.get("currencyCode") != "EUR":
        raise ValueError("Expected Portuguese catalog in EUR")
    plans = data.get("plans")
    addons = data.get("addons")
    if not isinstance(plans, list) or not isinstance(addons, list):
        raise ValueError("Missing catalog plans or addons")
    plan = next((x for x in plans if isinstance(x, dict) and x.get("planCode") == PLAN), None)
    if plan is None:
        raise ValueError("KS-LE-6 missing from catalog")
    base = monthly_price(plan)
    if base is None:
        return {}
    add_by_code = {a.get("planCode"): a for a in addons if isinstance(a, dict) and isinstance(a.get("planCode"), str)}
    families = [f for f in plan.get("addonFamilies", []) if isinstance(f, dict) and f.get("mandatory")]
    if not families or not any(f.get("name") == "storage" for f in families):
        return {}
    results = {}
    storage_family = next(f for f in families if f.get("name") == "storage")
    for code in storage_family.get("addons", []):
        if not isinstance(code, str) or not code.endswith("-" + PLAN):
            continue
        total = base
        valid = True
        for family in families:
            options = family.get("addons", [])
            selected = code if family.get("name") == "storage" else family.get("default")
            if selected not in options or selected not in add_by_code:
                valid = False
                break
            amount = monthly_price(add_by_code[selected])
            if amount is None:
                valid = False
                break
            total += amount
        if valid:
            # Exact storage code is the catalogue addon code with suffix stripped.
            stock_code = code[: -(len(PLAN) + 1)]
            results[stock_code] = f"{total:.2f} EUR/mês sem IVA (estimativa de catálogo)"
    return results


def fetch_catalog(timeout: int = 20) -> dict:
    request = Request(CATALOG, headers={"Accept": "application/json", "User-Agent": "ksle-monitor/1.2"})
    with urlopen(request, timeout=timeout) as response:
        blob = response.read(25_000_001)
    if len(blob) > 25_000_000:
        raise ValueError("Catalog response too large")
    return json.loads(blob)


def read_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(obj, dict) or not isinstance(obj.get("checked_at"), (int, float)):
            return {}
        return obj
    except (OSError, ValueError):
        LOG.warning("Pricing cache unreadable; rebuilding")
        return {}


def save_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".pricing-", delete=False) as f:
            tmp = Path(f.name)
            os.chmod(tmp, 0o600)
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def cached_prices(path: Path, now: float | None = None) -> dict[str, str]:
    """Read cache or refresh once per six hours. Never block stock alerts on errors."""
    now = time.time() if now is None else now
    old = read_cache(path)
    checked = old.get("checked_at", 0)
    cached = old.get("prices", {})
    cached = cached if isinstance(cached, dict) else {}
    if now - checked < (TTL_SECONDS if cached else RETRY_SECONDS):
        return cached
    try:
        prices = extract_prices(fetch_catalog())
        save_cache(path, {"checked_at": now, "prices": prices})
        LOG.info("Catalog prices refreshed: %d verified storage layouts", len(prices))
        return prices
    except Exception as exc:
        LOG.warning("Price catalog unavailable; omitting unverified prices: %s", exc)
        # If old quotes are stale, DO NOT surface them as current.
        try:
            save_cache(path, {"checked_at": now, "prices": {}})
        except OSError:
            LOG.warning("Unable to save price-cache retry timestamp")
        return {}
