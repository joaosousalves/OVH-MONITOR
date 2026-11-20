"""Bounded, value-free schema tracing for one MANUAL temporary OVH cart.

All field names and labels in the report come from constants in this file.
Never serialize a provider-supplied key, value, identifier, URL or exception.
This module has no network, checkout, payment or persistence operations.
"""

from __future__ import annotations

from collections import Counter
from typing import Any


STAGES = frozenset({
    "base_item", "addon_item", "checkout", "items_get", "items_expanded",
    "configurations_get", "configurations_expanded", "readback_unavailable",
    "readback_conflict", "readback_not_requested",
})
MAX_EVENTS = 32
MAX_ROWS = 24
KNOWN_LABELS = ("RENEW", "INSTALLATION", "DURATION", "DISCOUNT")
KNOWN_DETAIL_TYPES = ("INSTALLATION", "DURATION", "DISCOUNT", "GIFT", "PROMOTION")
KNOWN_CONFIG_LABELS = ("dedicated_datacenter", "region", "dedicated_os")
ITEM_FIELDS = ("itemId", "planCode", "duration", "pricingMode", "quantity")
CONFIG_FIELDS = ("configurationId", "label", "value")
PRICE_FIELDS = ("value", "currencyCode", "text")


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "list"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return "other"


def _fields(value: Any, names: tuple[str, ...]) -> str:
    if not isinstance(value, dict):
        return "not an object"
    return ", ".join(f"{name}:{_kind(value[name]) if name in value else 'missing'}"
                     for name in names)


def _list_shape(data: Any) -> str:
    if not isinstance(data, list):
        return _kind(data)
    sample = data[:MAX_ROWS]
    kinds = Counter(_kind(value) for value in sample)
    summary = ", ".join(f"{kind}={kinds[kind]}" for kind in
                        ("object", "number", "string", "bool", "null", "list", "other")
                        if kinds[kind])
    return f"list({len(data)}); sampled={len(sample)} [{summary}]"


def _categories(rows: Any, field: str, known: tuple[str, ...]) -> str:
    if not isinstance(rows, list):
        return _kind(rows)
    counts = Counter()
    for row in rows[:MAX_ROWS]:
        if isinstance(row, dict) and isinstance(row.get(field), str):
            counts[row[field] if row[field] in known else "OTHER"] += 1
        else:
            counts["MISSING_OR_INVALID"] += 1
    parts = [f"{name}={counts[name]}" for name in (*known, "OTHER", "MISSING_OR_INVALID")
             if counts[name]]
    suffix = "; truncated" if len(rows) > MAX_ROWS else ""
    return f"{len(rows)} row(s) [{', '.join(parts) or 'none'}]{suffix}"


def _item(data: Any) -> str:
    if not isinstance(data, dict):
        return _kind(data)
    prices = data.get("prices")
    parts = ["fields=[" + _fields(data, ITEM_FIELDS) + "]",
             "prices=" + (_categories(prices, "label", KNOWN_LABELS)
                          if "prices" in data else "missing")]
    if isinstance(prices, list):
        renewals = [row for row in prices[:MAX_ROWS]
                    if isinstance(row, dict) and row.get("label") == "RENEW"]
        if renewals:
            first = renewals[0]
            parts.append("RENEW[0]=[" + _fields(first, ("label", "price", "priceWithTax")) + "]")
            parts.append("RENEW[0].price=[" + _fields(first.get("price"), PRICE_FIELDS) + "]")
            parts.append("RENEW[0].priceWithTax=[" + _fields(first.get("priceWithTax"), PRICE_FIELDS) + "]")
    return "; ".join(parts)


def _checkout(data: Any) -> str:
    if not isinstance(data, dict):
        return _kind(data)
    prices = data.get("prices")
    parts = ["fields=[" + _fields(data, ("prices", "details", "contracts", "commitmentDuration")) + "]"]
    if isinstance(prices, dict):
        parts.append("prices=[" + _fields(prices, ("withoutTax", "tax", "withTax")) + "]")
        for name in ("withoutTax", "tax", "withTax"):
            parts.append(f"prices.{name}=[" + _fields(prices.get(name), PRICE_FIELDS) + "]")
    parts.append("details=" + (_categories(data.get("details"), "detailType", KNOWN_DETAIL_TYPES)
                               if "details" in data else "missing"))
    details = data.get("details")
    if isinstance(details, list) and details:
        first = details[0]
        parts.append("details[0]=[" + _fields(first, ("detailType", "totalPrice", "unitPrice")) + "]")
        if isinstance(first, dict):
            parts.append("details[0].totalPrice=[" + _fields(first.get("totalPrice"), PRICE_FIELDS) + "]")
    parts.append("contracts=" + (_list_shape(data.get("contracts")) if "contracts" in data else "missing"))
    return "; ".join(parts)


def _readback(data: Any, fields: tuple[str, ...], *, configurations: bool) -> str:
    summary = [_list_shape(data)]
    if isinstance(data, dict):
        summary.append("fields=[" + _fields(data, fields) + "]")
    if isinstance(data, list):
        patterns = Counter(_fields(row, fields) for row in data[:MAX_ROWS])
        for pattern, count in sorted(patterns.items())[:5]:
            summary.append(f"{count} row(s) fields=[{pattern}]")
        if len(patterns) > 5:
            summary.append("additional field shapes omitted")
        if configurations:
            summary.append("labels=" + _categories(data, "label", KNOWN_CONFIG_LABELS))
        else:
            price_shapes = Counter(
                _kind(row["prices"]) if isinstance(row, dict) and "prices" in row else "missing"
                for row in data[:MAX_ROWS]
            )
            summary.append("item prices=[" + ", ".join(
                f"{kind}={price_shapes[kind]}" for kind in
                ("list", "object", "missing", "null", "string", "number", "bool", "other")
                if price_shapes[kind]
            ) + "]")
            sample = next((row["prices"] for row in data[:MAX_ROWS]
                           if isinstance(row, dict) and isinstance(row.get("prices"), list)), None)
            if sample is not None:
                summary.append("first item labels=" + _categories(sample, "label", KNOWN_LABELS))
    return "; ".join(summary)


class SchemaTrace:
    """Keep a short, already-redacted report; never retain raw API responses."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.discarded = 0

    def capture(self, stage: str, payload: Any = None) -> None:
        if stage not in STAGES:
            return
        if len(self.lines) >= MAX_EVENTS:
            self.discarded += 1
            return
        if stage in ("base_item", "addon_item"):
            result = _item(payload)
        elif stage == "checkout":
            result = _checkout(payload)
        elif stage in ("items_get", "items_expanded"):
            result = _readback(payload, ITEM_FIELDS + ("prices",), configurations=False)
        elif stage in ("configurations_get", "configurations_expanded"):
            result = _readback(payload, CONFIG_FIELDS, configurations=True)
        else:
            result = "read-only identity evidence unavailable" if stage == "readback_unavailable" else (
                "read-only identity mismatch" if stage == "readback_conflict" else "no exact FQN supplied"
            )
        self.lines.append(stage + ": " + result)

    def report(self) -> str:
        lines = ["REDACTED MANUAL CART SCHEMA (NO PURCHASE):", *self.lines]
        if self.discarded:
            lines.append("Additional diagnostic stages omitted.")
        lines.append("Field names/categories are from a fixed allowlist; no provider values, IDs, URLs or credentials.")
        lines.append("Schema is observational only; VAT, renewal, commitment and cart identity remain subject to independent checks. Checkout/payment DISABLED.")
        return "\n".join(lines)
