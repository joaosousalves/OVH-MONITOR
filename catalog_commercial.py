"""Read-only exact offer commercial evidence. No cart/order/payment mutations."""
from decimal import Decimal, ROUND_HALF_UP
import json
from confirmed_terms import VAT, for_storage
from ksle_monitor import PLAN_CODE, REQUIRED_MEMORY, STORAGE_PRIORITIES, ALLOWED_DATACENTERS
from order_probe import catalog_selection, OrderProbeError
from pricing import fetch_catalog
from commitment_guard import inspect_commitment, CommitmentSafetyError


def audit_offer(catalog, fqn, datacenter):
    if fqn not in {f"{PLAN_CODE}.{REQUIRED_MEMORY}.{s}" for s in STORAGE_PRIORITIES}:
        raise ValueError("Only approved KS-LE-6 configurations")
    if datacenter not in ALLOWED_DATACENTERS:
        raise ValueError("Unapproved datacenter")
    locale = catalog.get("locale", {})
    if locale.get("currencyCode") != "EUR" or locale.get("subsidiary", "PT") != "PT":
        raise ValueError("Not PT/EUR catalog")
    _, addons = catalog_selection(catalog, "default", datacenter, target_fqn=fqn)
    results = []
    total = Decimal(0)
    for code, collection in [(PLAN_CODE, "plans"), *((a, "addons") for a in addons)]:
        matches = [p for p in catalog.get(collection, []) if isinstance(p, dict) and p.get("planCode") == code]
        if len(matches) != 1:
            raise ValueError("Missing/ambiguous catalog component: " + code)
        rows = []
        for r in matches[0].get("pricings", []):
            if not isinstance(r, dict):
                continue
            if (r.get("phase") == 1 and r.get("interval") == 1
                    and r.get("intervalUnit") == "month" and r.get("mode") == "default"):
                rows.append(r)
        # Report observed fields for review rather than treating missing
        # mode, commitment, tax or alternative durations as approval.
        safe_rows = [{k: r.get(k) for k in ("phase", "interval", "intervalUnit", "mode",
                       "commitment", "price", "tax", "type")}
                     for r in matches[0].get("pricings", []) if isinstance(r, dict)][:24]
        valid = []
        for r in rows:
            if type(r.get("commitment")) is not int or r["commitment"] != 0:
                continue
            if r.get("engagementConfiguration") not in (None, {}):
                continue
            inspect_commitment(r)
            if type(r.get("price")) is not int or r["price"] < 0:
                continue
            valid.append(r["price"])
        verified = bool(valid) and len(set(valid)) == 1 and len(valid) == len(rows)
        component = {"code": code, "verified": verified, "pricingRows": safe_rows}
        if verified:
            net = Decimal(valid[0]) / Decimal(100000000)
            component["monthlyNetEUR"] = str(net)
            total += net
        results.append(component)
    report = {"fqn": fqn, "datacenter": datacenter, "catalogTaxRate": locale.get("taxRate"),
              "components": results}
    if all(r["verified"] for r in results):
        monthly_gross = (total * (1 + VAT)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        report.update(monthlyNetEUR=str(total), monthlyGrossEUR=str(monthly_gross),
                      notAboveReference=monthly_gross <= for_storage(fqn.split(".")[2]).monthly_gross,
                      evidence="explicit zero commitment, default monthly catalog components; VAT estimated at PT 23%")
    else:
        report["evidence"] = "incomplete catalog pricing/commitment evidence; no authorization"
    return report


def main():
    catalog = fetch_catalog()
    for storage in STORAGE_PRIORITIES:
        fqn = f"{PLAN_CODE}.{REQUIRED_MEMORY}.{storage}"
        # Prefer the first datacenter with an exact catalog configuration,
        # not a stock requirement. Read-only audit.
        last_error = None
        for dc in ALLOWED_DATACENTERS:
            try:
                print(json.dumps(audit_offer(catalog, fqn, dc), ensure_ascii=False, indent=2))
                break
            except (ValueError, OrderProbeError, CommitmentSafetyError) as exc:
                last_error = str(exc)
        else:
            print(f"{fqn}: catalog validation incomplete: {last_error}")
    print("PUBLIC CATALOG ONLY: no cart, no order, no payment; not a renewal invoice or checkout approval.")


if __name__ == "__main__":
    main()
