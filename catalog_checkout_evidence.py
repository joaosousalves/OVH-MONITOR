"""Conservative live quote reconciliation using observed Eco catalog fields.

The result is the current catalog/first-period price, not a guarantee of
future seller price changes. No checkout, credentials or network operations.
"""
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from confirmed_terms import VAT
from catalog_commercial import audit_offer
from commercial_audit import validate_initial_price
from commitment_guard import inspect_commitment
from purchase_guard import PurchaseSafetyError, parse_eur

SCALE = Decimal(100000000)
CENT = Decimal(".01")


def _round(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def catalog_quote_evidence(catalog, fqn, dc, quote, *, max_initial, max_monthly):
    report = audit_offer(catalog, fqn, dc)
    if report["catalogTaxRate"] != 23 or not report.get("notAboveReference"):
        raise PurchaseSafetyError("Catalog monthly offer/commitment or PT tax unverified")
    monthly = []
    setup = []
    monthly_tax = Decimal(0)
    setup_tax = Decimal(0)
    for index, component in enumerate(report["components"]):
        if not component["verified"]:
            raise PurchaseSafetyError("Incomplete mandatory catalog component")
        collection = "plans" if index == 0 else "addons"
        matches = [r for r in catalog[collection] if isinstance(r, dict)
                   and r.get("planCode") == component["code"]]
        if len(matches) != 1 or not isinstance(matches[0].get("pricings"), list):
            raise PurchaseSafetyError("Catalog component changed or missing")
        default_rows = [r for r in matches[0]["pricings"] if isinstance(r, dict)
                        and r.get("mode") == "default"]
        if any(type(r.get("phase")) is not int or r["phase"] not in (0, 1) for r in default_rows):
            raise PurchaseSafetyError("Unexpected default pricing phase")
        for phase, interval, unit, amounts in ((1, 1, "month", monthly), (0, 0, "none", setup)):
            rows = [r for r in default_rows if r["phase"] == phase]
            valid = set()
            for row in rows:
                inspect_commitment(row)
                price, tax = row.get("price"), row.get("tax")
                if (row.get("interval") != interval or row.get("intervalUnit") != unit
                        or type(row.get("commitment")) is not int or row["commitment"] != 0
                        or row.get("type") != "rental"
                        or type(price) is not int or price < 0
                        or type(tax) is not int or tax < 0
                        or Decimal(tax) != Decimal(price) * VAT):
                    raise PurchaseSafetyError("Catalog default charge/tax/commitment conflict")
                valid.add((price, tax))
            # Repeated identical zero setup rows are observed on bandwidth.
            # Multiple nonzero rows remain ambiguous even if identical.
            if len(valid) != 1 or (len(rows) != 1 and valid != {(0, 0)}):
                raise PurchaseSafetyError("Missing or ambiguous catalog component charge")
            price, tax = next(iter(valid))
            amount = Decimal(price) / SCALE
            if amount != _round(amount):
                raise PurchaseSafetyError("Non-cent catalog price")
            amounts.append(amount)
            if phase == 1:
                monthly_tax += Decimal(tax) / SCALE
            else:
                setup_tax += Decimal(tax) / SCALE
    monthly_net = sum(monthly, Decimal(0))
    setup_net = sum(setup, Decimal(0))
    gross_monthly = _round(monthly_net + monthly_tax)
    if not 0 < gross_monthly <= max_monthly:
        raise PurchaseSafetyError("Catalog monthly price above approved cap/reference")
    initial = validate_initial_price(quote, max_initial)
    inspect_commitment(quote)
    if (initial.without_tax != monthly_net + setup_net
            or initial.vat != _round(monthly_tax + setup_tax)):
        raise PurchaseSafetyError("Checkout initial tax/price differs from catalog")
    details = quote.get("details")
    if not isinstance(details, list) or not 0 < len(details) <= 24:
        raise PurchaseSafetyError("Missing bounded checkout detail rows")
    observed = {"DURATION": [], "INSTALLATION": []}
    for row in details:
        if not isinstance(row, dict) or row.get("detailType") not in observed or type(row.get("quantity")) is not int or row["quantity"] != 1:
            raise PurchaseSafetyError("Unexpected checkout charge type or quantity")
        amounts = []
        for key in ("unitPrice", "totalPrice"):
            field = row.get(key)
            if not isinstance(field, dict) or field.get("currencyCode") != "EUR":
                raise PurchaseSafetyError("Checkout detail currency/price missing")
            amounts.append(parse_eur(field.get("value"), label="checkout detail", allow_zero=True))
        if amounts[0] != amounts[1]:
            raise PurchaseSafetyError("Checkout unit and total price differ")
        observed[row["detailType"]].append(amounts[0])
    if (Counter(observed["DURATION"]) != Counter(monthly)
            or Counter(v for v in observed["INSTALLATION"] if v)
            != Counter(v for v in setup if v)):
        raise PurchaseSafetyError("Checkout duration/installation charges differ from catalog")
    return initial, gross_monthly
