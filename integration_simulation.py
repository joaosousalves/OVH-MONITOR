"""Offline integration rehearsal: stock -> ranking -> quote -> email -> dedupe.

All remote dependencies are replaced by explicit fake callbacks. Even with
OVH credentials in the environment, this module never creates an API client,
performs a network request, sends SMTP, submits checkout, or charges payment.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import tempfile
from unittest.mock import patch

import commercial_audit
import ksle_monitor as monitor
from account_reconciliation import AccountInventory
from auto_buy_guard import PurchaseEvidence, assess_auto_buy
from commercial_watch import watch_once
from payment_evidence import PaymentEvidence
from pending_orders import PendingOrderInventory
from purchase_guard import (
    APPROVED_FQNS, STORAGE_PRIORITIES, OrderJournal, PurchasePolicy,
    PurchaseSafetyError, choose_candidate,
)
from purchase_quota import PurchaseQuota


def _demo_policy() -> PurchasePolicy:
    return PurchasePolicy.from_env({
        "OVH_AUTO_BUY_ENABLED": "false",
        "OVH_AUTO_BUY_MAX_TOTAL_EUR": "80.00",
        "OVH_AUTO_BUY_MAX_MONTHLY_EUR": "37.00",
        "OVH_AUTO_BUY_MAX_SERVERS": "2",
    })


def _stock(index: int, dc: str, status: str) -> monitor.Stock:
    return monitor.Stock(APPROVED_FQNS[index], STORAGE_PRIORITIES[index], dc, status)


def _fake_cart(_client, *, quote_validator, renewal_observer, **_kwargs):
    """A fabricated gross quote + P1M line, with no OVH calls at all."""
    quote_validator({
        "prices": {
            "withoutTax": {"currencyCode": "EUR", "value": "40.00"},
            "tax": {"currencyCode": "EUR", "value": "9.20"},
            "withTax": {"currencyCode": "EUR", "value": "49.20"},
        },
        "details": [
            {"detailType": "DURATION", "totalPrice": {"currencyCode": "EUR", "value": "35.00"}},
            {"detailType": "INSTALLATION", "totalPrice": {"currencyCode": "EUR", "value": "5.00"}},
        ],
        "commitmentDuration": "P0M",  # Fake only, not contract proof.
    })
    renewal_observer(({
        "duration": "P1M",
        "engagementMonths": 0,
        "prices": [{
            "label": "RENEW",
            "price": {"currencyCode": "EUR", "value": "29.00"},
            "priceWithTax": {"currencyCode": "EUR", "value": "35.67"},
        }],
    },))
    return "SIMULATION ONLY: fake cart; no remote cart created"


def _inventory(servers: int) -> PendingOrderInventory:
    return PendingOrderInventory(AccountInventory(servers, 15, 2), 89, 0, 0, 22)


def simulate_restock_flow() -> list[str]:
    """Exercise actual monitor/watcher/audit guards in an isolated temp dir.

    Raises AssertionError if stock SMTP dedupe, watcher persistence, disk
    ordering, quota enforcement, or checkout-blocking invariants regress.
    """
    output = ["[SIMULATION ONLY] Offline fake OVH/SMTP; no order, payment or real email."]
    policy = _demo_policy()
    fake_messages: list[tuple[str, str]] = []
    initial = [_stock(0, "bhs", "unavailable"), _stock(1, "fra", "unavailable")]
    fallback = [_stock(0, "bhs", "unavailable"), _stock(1, "fra", "1H-low")]
    preferred = [_stock(0, "bhs", "1H-low"), _stock(1, "fra", "1H-low")]
    with tempfile.TemporaryDirectory(prefix="ksle-offline-flow-") as directory:
        root = Path(directory)
        alert_file = root / "stock.json"
        watch_file = root / "watch.json"
        journal = OrderJournal(root / "intent.json")
        quota = PurchaseQuota(root / "quota.json")

        def fake_mail(_config, subject, body, **_kwargs):
            fake_messages.append(("stock", subject))

        fake_config = monitor.MailConfig("", 0, "", "", "", "", ())

        def fake_notify(candidate: monitor.Stock, report: str) -> None:
            assert "AUTO-BUY BLOCKED" in report
            assert "INITIAL CART DETAIL LINES" in report
            assert "EVIDENCE CHECKLIST (NO PURCHASE)" in report
            assert "Checkout/payment remain DISABLED" in report
            fake_messages.append(("commercial", candidate.key))

        def audit(stocks: list[monitor.Stock], slots_used: int = 0) -> str:
            # Patching only the fake cart/public refresh inside the real audit
            # enforces no accidental network access, even if credentials exist.
            with patch.object(commercial_audit, "test_order", side_effect=_fake_cart):
                with patch.object(commercial_audit, "verify_available_test_fqn"):
                    return commercial_audit.audit_commercial(
                        policy, stocks=stocks, client=object(), journal=journal,
                        quota=quota,
                        pending_check=lambda _client, _max: _inventory(slots_used),
                        payment_check=lambda _client: PaymentEvidence("VALID_DEFAULT"),
                    )

        with patch.object(monitor, "send_mail", side_effect=fake_mail):
            monitor.run_once(initial, alert_file, fake_config, False)
            assert not fake_messages
            assert watch_once(policy, stocks=initial, path=watch_file,
                              audit=lambda: audit(initial), notify=fake_notify, now=1000).startswith("WAITING")
            output.append("1. Indisponível: 0 alertas; nenhum carrinho (simulado).")

            assert choose_candidate(fallback, policy) == fallback[1]
            monitor.run_once(fallback, alert_file, fake_config, False)
            assert watch_once(policy, stocks=fallback, path=watch_file,
                              audit=lambda: audit(fallback), notify=fake_notify, now=2000).startswith("SENT")
            output.append("2. Restock FRA, discos #2: email de stock + auditoria fictícios; preço inicial 49,20 €.")

            monitor.run_once(fallback, alert_file, fake_config, False)
            assert watch_once(policy, stocks=fallback, path=watch_file,
                              audit=lambda: audit(fallback), notify=fake_notify, now=2050).startswith("UNCHANGED")
            assert len(fake_messages) == 2
            output.append("3. Mesmo stock repetido: 0 emails duplicados (estado persistente).")

            assert choose_candidate(preferred, policy) == preferred[0]
            monitor.run_once(preferred, alert_file, fake_config, False)
            assert watch_once(policy, stocks=preferred, path=watch_file,
                              audit=lambda: audit(preferred), notify=fake_notify, now=3000).startswith("SENT")
            assert len(fake_messages) == 4
            output.append("4. Chega BHS, discos #1: seleção por prioridade; mais 2 emails fictícios.")

            # A verified 2/2 count stops the audit before any fake cart.
            at_cap = audit(preferred, slots_used=2)
            assert at_cap.startswith("LIMIT REACHED")
            assert not journal.path.exists() and quota.read() == []
            output.append("5. Quota 2/2: auditoria interrompida antes do carrinho.")

        blocked_price = assess_auto_buy(policy, preferred[0], PurchaseEvidence(
            initial_including_vat=Decimal("49.20"), initial_quote_verified=True,
            monthly_including_vat=Decimal("38.00"), monthly_renewal_verified=True,
            no_fixed_commitment_verified=True, payment_method_ready=True,
            remote_slots_used=0, remote_inventory_verified=True,
            local_purchase_history_clear=True,
        ))
        assert not blocked_price.simulated_criteria_met
        assert "monthly EUR amount exceeds" in blocked_price.summary()
        unknown_price = assess_auto_buy(policy, preferred[0], PurchaseEvidence(
            initial_including_vat=Decimal("49.20"), initial_quote_verified=True,
            remote_slots_used=0, remote_inventory_verified=True,
            local_purchase_history_clear=True,
        ))
        assert "final monthly renewal including VAT not verified" in unknown_price.summary()
        try:
            commercial_audit.validate_initial_price({"prices": {
                "withoutTax": {"currencyCode": "EUR", "value": "70.00"},
                "tax": {"currencyCode": "EUR", "value": "11.00"},
                "withTax": {"currencyCode": "EUR", "value": "81.00"},
            }}, policy.max_total_eur)
        except PurchaseSafetyError as exc:
            assert "PRICE BLOCKED" in str(exc)
        else:
            raise AssertionError("Initial EUR cap did not block an over-budget price")
        output.append("6. 38 €/mês, 81 € iniciais ou mensalidade desconhecida: guardrails BLOQUEIAM.")
        assert sum(kind == "stock" for kind, _ in fake_messages) == 2
        assert sum(kind == "commercial" for kind, _ in fake_messages) == 2
        output.append("PASS: 2 alertas de stock + 2 auditorias simuladas; 0 emails reais; 0 compras.")
    return output


def run_restock_simulation() -> int:
    for line in simulate_restock_flow():
        print(line)
    return 0
