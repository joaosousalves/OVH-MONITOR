"""Conservative, cached public OVH Eco monthly pricing (no cart, no purchase).

Return None unless the monthly base plan AND all mandatory addons have an
unambiguous public price. Values are catalog estimates *excluding VAT*, not
checkout totals. Uses only Python standard library.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import json
import logging
import os
import re
import tempfile
import time
from urllib.request import Request, urlopen

PLAN = "27skle06-v1"
CATALOG = "https://api.eu.ovhcloud.com/v1/order/catalog/public/eco?ovhSubsidiary=PT"
TTL_SECONDS = 6 * 3600
RETRY_SECONDS = 3600  # Avoid hammering catalog on failure.
CACHE_SCHEMA_VERSION = 2  # Invalidate pre-fix cache entries with zero parsed prices.
LOG = logging.getLogger("ksle-monitor")
PT_MAINLAND_VAT_RATE = Decimal("0.23")
# Publicly advertised KS-LE-6 base installation fee (Eco PT catalogue,
# checked 2026-11-20). Not a configuration-specific or final checkout charge.
INDICATIVE_SETUP_FEE_NET = Decimal("26.99")
_CATALOG_EUR = re.compile(
    r"^(0|[1-9][0-9]{0,5})\.([0-9]{2}) EUR/mês sem IVA \(estimativa de catálogo\)$"
)


def indicative_pt_vat_gross(description: str) -> Decimal | None:
    """Apply the owner's 23% PT mainland assumption to a PUBLIC estimate.

    Never use the result as a purchase authorization or a verified renewal
    amount. Unknown, malformed or non-catalog prices must remain unverified.
    """
    if not isinstance(description, str):
        return None
    match = _CATALOG_EUR.fullmatch(description)
    if not match:
        return None
    net = Decimal(f"{match.group(1)}.{match.group(2)}")
    if net <= 0:
        return None
    return (net * (Decimal("1.00") + PT_MAINLAND_VAT_RATE)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )


def indicative_initial_total(description: str) -> Decimal | None:
    """Illustrative first month + published base setup fee, not an order quote.

    Calculation assumes a single month's recurring payment at checkout and
    the advertised base installation fee without any per-layout adjustment.
    Actual fees, discounts and taxes must be obtained from the real cart.
    """
    gross_monthly = indicative_pt_vat_gross(description)
    if gross_monthly is None:
        return None
    gross_setup = (INDICATIVE_SETUP_FEE_NET * (
        Decimal("1.00") + PT_MAINLAND_VAT_RATE
    )).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return gross_monthly + gross_setup


def annotate_pt_vat_estimate(description: str) -> str:
    """Add a prominently qualified VAT estimate; keep original cache intact."""
    gross = indicative_pt_vat_gross(description)
    if gross is None:
        return description
    initial = indicative_initial_total(description)
    return (
        f"{description} | {gross:.2f} EUR/mês com IVA de 23% "
        f"| ~{initial:.2f} EUR primeiro pagamento com IVA (1 mês + "
        "instalação base anunciada de 26.99 EUR sem IVA; sem extras) "
        "(estimativas; NÃO é cotação final nem prova de sem compromisso)"
    )


def monthly_price(item: dict) -> Decimal | None:
    """Monthly recurring EUR price from the OVH Eco catalogue.

    Current Eco JSON identifies recurring one-month prices by phase=1 and
    interval=1 (unit 'month' when supplied). Phase=0/interval=0 is a
    separate initial charge, NOT the recurring monthly price. Older catalog
    formats may instead use duration=P1M/pricingMode=default.
    Some addon catalogs repeat an identical monthly price in multiple rows.
    """
    if not isinstance(item, dict):
        return None
    matches = []
    for row in item.get("pricings", []):
        if not isinstance(row, dict):
            continue
        amount = row.get("price")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            continue
        if "interval" in row or "phase" in row:
            monthly = (
                row.get("phase") == 1
                and row.get("interval") == 1
                and row.get("intervalUnit", "month") == "month"
            )
        else:
            monthly = (
                row.get("duration") == "P1M"
                and row.get("pricingMode") == "default"
            )
        if monthly:
            matches.append(amount)
    # Duplicates with the same amount are harmless; conflicting prices are not.
    if not matches or len(set(matches)) != 1:
        return None
    return Decimal(matches[0]) / Decimal(100000000)


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
    if old.get("schema_version") == CACHE_SCHEMA_VERSION and now - checked < (TTL_SECONDS if cached else RETRY_SECONDS):
        return cached
    try:
        prices = extract_prices(fetch_catalog())
        save_cache(path, {"schema_version": CACHE_SCHEMA_VERSION, "checked_at": now, "prices": prices})
        LOG.info("Catalog prices refreshed: %d verified storage layouts", len(prices))
        return prices
    except Exception as exc:
        LOG.warning("Price catalog unavailable; omitting unverified prices: %s", exc)
        # If old quotes are stale, DO NOT surface them as current.
        try:
            save_cache(path, {"schema_version": CACHE_SCHEMA_VERSION, "checked_at": now, "prices": {}})
        except OSError:
            LOG.warning("Unable to save price-cache retry timestamp")
        return {}


def diagnostic_summary(data: dict) -> dict:
    """Safe, limited structure summary for troubleshooting catalog parsing.

    Prints no authentication credentials and no full catalog. Raw identifiers
    and pricing fields are restricted to this KS-LE-6 and its mandatory addons.
    """
    result = {"currency": (data.get("locale") or {}).get("currencyCode"),
              "plan_found": False, "base_pricings": [], "families": []}
    plans = data.get("plans") or []
    target = next((p for p in plans if isinstance(p, dict) and p.get("planCode") == PLAN), None)
    if target is None:
        return result
    result["plan_found"] = True
    result["base_pricings"] = [
        {k: row.get(k) for k in ("duration", "pricingMode", "phase", "price", "interval")
         if k in row}
        for row in (target.get("pricings") or []) if isinstance(row, dict)
    ][:12]
    addons = {a.get("planCode"): a for a in (data.get("addons") or []) if isinstance(a, dict)}
    for family in target.get("addonFamilies") or []:
        if not isinstance(family, dict) or not family.get("mandatory"):
            continue
        name = family.get("name")
        keys = family.get("addons") or []
        code = keys[0] if name == "storage" and keys else family.get("default")
        ref = addons.get(code, {})
        result["families"].append({
            "family": name, "default": family.get("default"),
            "sample_code": code, "option_count": len(keys),
            "sample_present": bool(ref),
            "sample_pricings": [
                {k: row.get(k) for k in ("duration", "pricingMode", "phase", "price", "interval")
                 if k in row}
                for row in (ref.get("pricings") or []) if isinstance(row, dict)
            ][:6],
        })
    return result
