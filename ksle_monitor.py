#!/usr/bin/env python3
"""Monitor the public OVHcloud dedicated-server stock API; alert by SMTP.

Runs once per invocation. A systemd timer handles scheduling. No third-party
Python dependencies or OVHcloud API credentials are needed.
"""

from __future__ import annotations

import argparse
from email.utils import parsedate_to_datetime
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
import json
import logging
import os
from pathlib import Path
import re
import smtplib
import ssl
import sys
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PLAN_CODE = "27skle06-v1"
PREFERRED_STORAGE = "hybridsoftraid-2x4000sa-2x500nvme"
PRODUCT_URL = "https://eco.ovhcloud.com/pt/kimsufi/ks-le-6/"
API_URL = "https://api.eu.ovhcloud.com/v1/dedicated/server/datacenter/availabilities"
STORAGE_NAMES = {
    PREFERRED_STORAGE: "PREFERRED: 2 x 512 GB NVMe + 2 x 4 TB HDD",
    "softraid-2x1920nvme": "2 x 1.92 TB NVMe",
    "softraid-3x1000nvme": "3 x 1 TB NVMe",
    "softraid-4x960ssd": "4 x 960 GB SATA SSD",
}
# Only these 128 GB KS-LE-6 configurations in Europe or Canada are alert targets.
REQUIRED_MEMORY = "ram-128g-ddr4-ecc-2400"
# BHS Canada; FRA Germany (Limburg); GRA/RBX/SBG France; LON UK; WAW Poland.
ALLOWED_DATACENTERS = ("bhs", "fra", "gra", "lon", "rbx", "sbg", "waw")

# Official feed currently returns 1H-high, 1H-low, 72H, etc. Any H status
# indicates a provisioning lead time, not an inventory count. Other statuses
# like unavailable, unknown and comingSoon must NOT trigger an email.
AVAILABLE_STATUS = re.compile(r"^(?:\d+H(?:-(?:high|low))?|available|limited)$", re.I)
LOGGER = logging.getLogger("ksle-monitor")


class MonitorError(Exception):
    """Configuration, API, or saved-state error."""


class APITransientError(MonitorError):
    """Temporary API failure; store persistent cooldown before returning."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def retry_after_seconds(value: str | None, now: float | None = None) -> int | None:
    """Understand Retry-After as seconds or HTTP date, capped at one day."""
    if not value:
        return None
    value = value.strip()
    if value.isdecimal():
        return min(86400, max(0, int(value)))
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return None
        return min(86400, max(0, int(date.timestamp() - (time.time() if now is None else now) + 0.999)))
    except (ValueError, TypeError, OverflowError):
        return None



@dataclass(frozen=True)
class Stock:
    fqn: str
    storage: str
    datacenter: str
    status: str

    @property
    def key(self) -> str:
        return f"{self.fqn}@{self.datacenter}"

    @property
    def available(self) -> bool:
        return bool(AVAILABLE_STATUS.fullmatch(self.status))

    @property
    def preferred(self) -> bool:
        return self.storage == PREFERRED_STORAGE

    @property
    def storage_label(self) -> str:
        return STORAGE_NAMES.get(self.storage, self.storage)


def eligible_alert_stock(stock: Stock) -> bool:
    """Require exact KS-LE-6 model, 128 GB RAM, known disks and EU/CA site."""
    return (
        stock.storage in STORAGE_NAMES
        and stock.fqn == f"{PLAN_CODE}.{REQUIRED_MEMORY}.{stock.storage}"
        and stock.datacenter in ALLOWED_DATACENTERS
    )


def monitored_stocks(stocks: list[Stock]) -> list[Stock]:
    """Apply alert policy before computing notifications and persistent state."""
    return [stock for stock in stocks if eligible_alert_stock(stock)]


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    security: str
    username: str
    password: str
    sender: str
    recipients: tuple[str, ...]
    timeout: int = 15


def retrieve_stock(timeout_seconds: int = 15, plan_code: str = PLAN_CODE) -> list[Stock]:
    url = f"{API_URL}?{urlencode({'planCode': plan_code})}"
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "ksle-monitor/1.0"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            # Keep response bounded in case of an incorrect proxy response.
            data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise MonitorError("API response unexpectedly large")
        payload = json.loads(data)
    except HTTPError as exc:
        if exc.code == 429 or 500 <= exc.code < 600:
            retry = retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
            raise APITransientError(f"OVHcloud API HTTP {exc.code}", retry_after=retry) from exc
        raise MonitorError(f"OVHcloud API HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise APITransientError(f"Temporary OVHcloud connection error: {exc}") from exc
    except ValueError as exc:
        raise MonitorError(f"Invalid OVHcloud JSON response: {exc}") from exc
    return parse_stock(payload, plan_code=plan_code)


def parse_stock(payload: Any, plan_code: str = PLAN_CODE) -> list[Stock]:
    if not isinstance(payload, list):
        raise MonitorError("OVHcloud API returned non-list JSON")

    stocks: dict[str, Stock] = {}
    plan_rows = 0
    for row in payload:
        if not isinstance(row, dict) or row.get("planCode") != plan_code:
            continue
        plan_rows += 1
        fqn = row.get("fqn")
        storage = row.get("storage")
        datacenters = row.get("datacenters")
        if not isinstance(fqn, str) or not fqn.startswith(f"{plan_code}."):
            raise MonitorError("Unexpected or missing plan FQN in OVHcloud API")
        if not isinstance(storage, str) or not storage:
            raise MonitorError("Missing storage code in OVHcloud API")
        if not isinstance(datacenters, list):
            raise MonitorError("Missing datacenters in OVHcloud API")

        for dc in datacenters:
            if not isinstance(dc, dict):
                raise MonitorError("Invalid datacenter object in OVHcloud API")
            name, status = dc.get("datacenter"), dc.get("availability")
            if not isinstance(name, str) or not name.strip() or not isinstance(status, str):
                raise MonitorError("Missing datacenter name/status in OVHcloud API")
            stock = Stock(fqn=fqn, storage=storage, datacenter=name.lower(), status=status)
            if stock.key in stocks:
                raise MonitorError(f"Duplicate configuration in OVHcloud API: {stock.key}")
            stocks[stock.key] = stock

    if not plan_rows or not stocks:
        # An empty/partial API response must not reset alert history.
        raise MonitorError(f"No {plan_code} stock records in OVHcloud API response")
    return sorted(stocks.values(), key=stock_sort_key)


def stock_sort_key(stock: Stock) -> tuple[int, str, str]:
    return (not stock.preferred, stock.storage, stock.datacenter)


def state_path() -> Path:
    configured = os.getenv("STATE_FILE")
    if configured:
        return Path(configured).expanduser()
    systemd_dir = os.getenv("STATE_DIRECTORY")
    if systemd_dir:
        return Path(systemd_dir.split(":")[0]) / "state.json"
    return Path.home() / ".local/state/ovh-ksle-monitor/state.json"


def cooldown_path() -> Path:
    """Keep API rate-limit state separate from alert history."""
    return state_path().with_name("api-backoff.json")


def read_cooldown(path: Path) -> tuple[float, int]:
    if not path.exists():
        return 0.0, 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        until, failures = data["until"], data["failures"]
        if not isinstance(until, (int, float)) or not isinstance(failures, int) or failures < 0:
            raise ValueError("Invalid cooldown fields")
        return float(until), failures
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise MonitorError(f"Cannot load API cooldown from {path}: {exc}") from exc


def write_cooldown(path: Path, until: float, failures: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".api-backoff-", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            json.dump({"until": until, "failures": failures}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise MonitorError(f"Cannot save API cooldown: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def api_cooldown_active(path: Path, now: float | None = None) -> bool:
    until, _ = read_cooldown(path)
    remaining = until - (time.time() if now is None else now)
    if remaining > 0:
        LOGGER.warning("API cooldown active: skipping request for another %.0f seconds", remaining)
        return True
    return False


def apply_api_failure(path: Path, error: APITransientError, now: float | None = None) -> int:
    now = time.time() if now is None else now
    _, previous_failures = read_cooldown(path)
    failures = previous_failures + 1
    # Exponential backoff: 60, 120, 240, 480, 960, capped at 1800 seconds.
    delay = min(1800, 60 * (2 ** min(failures - 1, 5)))
    if error.retry_after is not None:
        delay = max(delay, error.retry_after)
    write_cooldown(path, now + delay, failures)
    LOGGER.warning("Temporary OVH API error: %s; pausing checks for %d seconds", error, delay)
    return delay


def load_alerted(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("version") != 1
                or not isinstance(data.get("alerted"), list)
                or not all(isinstance(key, str) for key in data["alerted"])):
            raise ValueError("Unexpected state structure")
        return set(data["alerted"])
    except (OSError, ValueError) as exc:
        # Stop rather than potentially sending duplicate emails on corruption.
        raise MonitorError(f"Cannot load alert history from {path}: {exc}") from exc


def save_alerted(path: Path, alerted: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = json.dumps({"version": 1, "alerted": sorted(alerted)}, indent=2) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".state-", delete=False
        ) as handle:
            temp_path = Path(handle.name)
            os.chmod(temp_path, 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        raise MonitorError(f"Cannot save alert history to {path}: {exc}") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def transition(stocks: list[Stock], previous: set[str]) -> tuple[list[Stock], set[str]]:
    """Find new stock events; preserve history for temporarily missing rows.

    Only an explicitly observed unavailable status resets a previously alerted
    configuration, so the next available transition can alert again.
    """
    by_key = {stock.key: stock for stock in stocks}
    still_alerted = {
        key for key in previous
        if key not in by_key or by_key[key].available
    }
    newly_available = [
        stock for stock in stocks if stock.available and stock.key not in previous
    ]
    return sorted(newly_available, key=stock_sort_key), still_alerted


def mail_config() -> MailConfig:
    host = os.getenv("SMTP_HOST", "").strip()
    sender = os.getenv("SMTP_FROM", "").strip()
    recipients = tuple(x.strip() for x in os.getenv("SMTP_TO", "").split(",") if x.strip())
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    security = os.getenv("SMTP_SECURITY", "starttls").lower()
    if not host or not sender or not recipients:
        raise MonitorError("SMTP_HOST, SMTP_FROM and SMTP_TO must be configured")
    if security not in {"starttls", "ssl"}:
        raise MonitorError("SMTP_SECURITY must be 'starttls' or 'ssl'")
    if bool(username) != bool(password):
        raise MonitorError("Set both SMTP_USERNAME and SMTP_PASSWORD, or neither")
    try:
        port = int(os.getenv("SMTP_PORT", "465" if security == "ssl" else "587"))
    except ValueError as exc:
        raise MonitorError("SMTP_PORT must be numeric") from exc
    if not 1 <= port <= 65535:
        raise MonitorError("SMTP_PORT outside valid range")
    try:
        timeout = int(os.getenv("SMTP_TIMEOUT", "15"))
    except ValueError as exc:
        raise MonitorError("SMTP_TIMEOUT must be numeric") from exc
    if not 1 <= timeout <= 300:
        raise MonitorError("SMTP_TIMEOUT must be between 1 and 300 seconds")
    return MailConfig(host, port, security, username, password, sender, recipients, timeout)


def send_mail(config: MailConfig, subject: str, body: str, html_body: str | None = None) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)
    message.set_content(body)
    if html_body is not None:
        message.add_alternative(html_body, subtype="html")
    context = ssl.create_default_context()
    try:
        if config.security == "ssl":
            with smtplib.SMTP_SSL(config.host, config.port, timeout=config.timeout, context=context) as server:
                if config.username:
                    server.login(config.username, config.password)
                refused = server.send_message(message)
        else:
            with smtplib.SMTP(config.host, config.port, timeout=config.timeout) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                if config.username:
                    server.login(config.username, config.password)
                refused = server.send_message(message)
        if refused:
            raise MonitorError(f"SMTP rejected some recipients: {', '.join(refused)}")
    except (smtplib.SMTPException, OSError) as exc:
        raise MonitorError(f"Email delivery failed: {exc}") from exc


def alert_content(stocks: list[Stock], prices: dict[str, str] | None = None) -> tuple[str, str]:
    preferred_count = sum(stock.preferred for stock in stocks)
    marker = " - preferred disks!" if preferred_count else ""
    subject = f"[OVH STOCK] KS-LE-6: {len(stocks)} new availability event(s){marker}"
    lines = [
        "OVHcloud KS-LE-6 is reported available in these configurations:",
        "",
    ]
    for stock in sorted(stocks, key=stock_sort_key):
        lines.extend([
            f"{'[PREFERRED] ' if stock.preferred else ''}{stock.storage_label}",
            f"  Datacenter: {stock.datacenter.upper()}",
            f"  API availability: {stock.status}",
            f"  Preço: {(prices or {}).get(stock.storage, 'Preço de catálogo indisponível; confirmar no checkout')}",
            "",
        ])
    lines.extend([
        f"Check and order: {PRODUCT_URL}",
        "",
        "This is an automated stock notification, not a reservation or purchase.",
        "Availability may change before checkout. OVHcloud confirms final stock.",
        f"Checked at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
    ])
    return subject, "\n".join(lines)



def alert_html(stocks: list[Stock], *, test: bool = False, prices: dict[str, str] | None = None) -> str:
    """Mobile-friendly table-based HTML email, escaped for untrusted API values."""
    from html import escape

    sorted_stocks = sorted(stocks, key=stock_sort_key)
    cards = []
    for stock in sorted_stocks:
        title = escape(stock.storage_label)
        dc = escape(stock.datacenter.upper())
        status = escape(stock.status)
        preferred = stock.preferred
        price = escape((prices or {}).get(stock.storage, "Preço indisponível — confirmar no checkout"))
        accent = "#16a34a" if preferred else "#64748b"
        badge = (
            '<span style="color:#166534;background:#dcfce7;padding:5px 9px;'
            'border-radius:12px;font-size:12px;font-weight:700;">DISCOS PREFERIDOS</span>'
            if preferred else ""
        )
        cards.append(
            '<tr><td style="padding:0 0 12px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #e2e8f0;border-left:4px solid {accent};'
            'border-radius:10px;background:#ffffff;"><tr><td style="padding:18px;">'
            f'<div style="margin-bottom:9px;">{badge}</div>'
            f'<div style="font-size:16px;font-weight:700;color:#0f172a;">{title}</div>'
            f'<div style="font-size:13px;color:#475569;margin-top:8px;">Datacenter: '
            f'<strong>{dc}</strong> &nbsp;·&nbsp; Estado: <strong>{status}</strong></div>'
            f'<div style="font-size:13px;color:#334155;margin-top:8px;">Preço: {price}</div>'
            '</td></tr></table></td></tr>'
        )
    timestamp = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    test_banner = (
        '<tr><td style="padding:12px 24px;background:#fef3c7;color:#92400e;'
        'font-weight:700;">EMAIL DE TESTE — disponibilidade simulada</td></tr>'
        if test else ""
    )
    return (
        '<!doctype html><html lang="pt"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;padding:24px 12px;background:#f1f5f9;'
        'font-family:Arial,Helvetica,sans-serif;color:#0f172a;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        '<tr><td align="center"><table role="presentation" width="100%" '
        'cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;'
        'border-radius:14px;overflow:hidden;">'
        '<tr><td style="padding:28px 24px;background:#0f172a;color:#ffffff;">'
        '<div style="font-size:13px;letter-spacing:2px;color:#93c5fd;">OVH-MONITOR</div>'
        '<h1 style="margin:12px 0 4px;font-size:24px;line-height:1.25;">'
        'KS-LE-6 disponível</h1>'
        '<div style="font-size:14px;color:#cbd5e1;">Alerta de disponibilidade OVHcloud</div>'
        '</td></tr>'
        + test_banner
        + '<tr><td style="padding:24px;">'
        '<p style="font-size:14px;line-height:1.6;margin:0 0 18px;">'
        'A OVHcloud comunicou disponibilidade nas seguintes configurações:</p>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        + "".join(cards)
        + '</table>'
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'style="margin:12px 0 14px;"><tr><td bgcolor="#2563eb" '
        'style="border-radius:8px;">'
        f'<a href="{escape(PRODUCT_URL, quote=True)}" target="_blank" '
        'style="display:block;padding:14px 20px;background:#2563eb;'
        'border-radius:8px;color:#ffffff;text-decoration:none;'
        'font-weight:700;font-size:14px;">Ver servidor na OVHcloud</a>'
        '</td></tr></table>'
        '<p style="margin:0 0 20px;font-size:12px;color:#64748b;'
        'line-height:1.6;">Se o botão não abrir, utiliza este link:<br>'
        f'<a href="{escape(PRODUCT_URL, quote=True)}" target="_blank" '
        'style="color:#2563eb;text-decoration:underline;">'
        f'{escape(PRODUCT_URL)}</a></p>'
        f'<p style="font-size:12px;line-height:1.6;color:#64748b;">'
        f'Verificado em {timestamp}. O stock pode mudar antes de concluir '
        'a encomenda. Este email não reserva nem compra servidores.</p>'
        '</td></tr>'
        '<tr><td style="padding:16px 24px;background:#f8fafc;'
        'font-size:12px;color:#64748b;">'
        'Mensagem automática do OVH-MONITOR · Não responder</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def run_once(stocks: list[Stock], path: Path, config: MailConfig | None, dry_run: bool, prices: dict[str, str] | None = None) -> int:
    stocks = monitored_stocks(stocks)
    previous = load_alerted(path)
    new, retained = transition(stocks, previous)
    currently_available = sum(stock.available for stock in stocks)
    LOGGER.info(
        "%s: %d configurations/datacenters, %d available, %d new alert(s)",
        PLAN_CODE, len(stocks), currently_available, len(new),
    )
    if dry_run:
        for stock in new:
            LOGGER.info("DRY RUN alert: %s | %s | %s", stock.storage_label, stock.datacenter.upper(), stock.status)
        return 0
    if config is None:
        raise MonitorError("SMTP settings required for a real monitoring run")

    if new:
        subject, body = alert_content(new, prices=prices)
        # Do not mark an alert as sent until SMTP accepts the message.
        # We also write the observed out-of-stock resets on SMTP failures.
        try:
            send_mail(config, subject, body, html_body=alert_html(new, prices=prices))
        except MonitorError:
            if retained != previous:
                save_alerted(path, retained)
            raise
        retained.update(stock.key for stock in new)
        LOGGER.info("Sent one alert email for %d newly available option(s)", len(new))
    if retained != previous:
        save_alerted(path, retained)
    return 0


def print_stock(stocks: list[Stock]) -> None:
    for stock in stocks:
        marker = " *preferred*" if stock.preferred else ""
        print(f"{stock.datacenter.upper():<6} {stock.status:<12} {stock.storage_label}{marker}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OVHcloud KS-LE-6 stock monitor")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="Check stock, email new availability, update history (default)")
    group.add_argument("--check", action="store_true", help="Check stock without sending email or changing history")
    group.add_argument("--list", action="store_true", help="Print all API-reported locations and configurations")
    group.add_argument("--list-test-stock", action="store_true",
                       help="Read-only stock check for --test-plan (KS-LE-1 or KS-LE-6)")
    group.add_argument("--list-eco-options", action="store_true",
                       help="Read-only live stock for KS-LE-1 to KS-LE-6; no credentials")
    group.add_argument("--purchase-quota-status", action="store_true",
                       help="Read-only local 0/2 lifetime purchase attempt ledger")
    group.add_argument("--list-ksle6-disks", action="store_true",
                       help="Read-only ordered KS-LE-6 128GB disk availability in Europe/Canada")
    group.add_argument("--list-prices", action="store_true", help="Show cached public monthly catalog estimates (no cart)")
    group.add_argument("--diagnose-prices", action="store_true", help="Inspect KS-LE-6 public catalog price structure without credentials")
    group.add_argument("--send-test-email", action="store_true", help="Send a test email (no stock check)")
    group.add_argument("--test-order", action="store_true", help="Test an Eco cart and quote; NEVER submit an order or pay")
    group.add_argument("--purchase-preview", action="store_true",
                       help="Phase B: KS-LE-6 preview with EUR price cap, NO checkout or payment")
    group.add_argument("--purchase-preflight", action="store_true",
                       help="Phase E: manual quote + payment readiness; NEVER submit checkout or pay")
    group.add_argument("--reconcile-account", action="store_true",
                       help="GET-only KS-LE-6 service inventory against 2-server limit; no cart/order")
    group.add_argument("--check-payment-readiness", action="store_true",
                       help="Phase C: GET-only check of default OVHcloud payment method")
    group.add_argument("--simulate-checkout", action="store_true",
                       help="Phase C: completely offline mock checkout, never charges")
    group.add_argument("--reconcile-order-status", action="store_true",
                       help="Phase C: GET-only recorded order status, never retries")
    parser.add_argument("--datacenter", default="bhs", help="Datacenter for --test-order (default: bhs)")
    parser.add_argument("--storage", default="preferred", help="Storage addon code for --test-order (default: preferred)")
    parser.add_argument("--test-plan", choices=("ks-le-6", "ks-le-1"), default="ks-le-6",
                        help="Product for manual --test-order only; stock timer remains KS-LE-6")
    parser.add_argument("--test-fqn", default=None,
                        help="Exact stock FQN for manual cart test; selects its RAM and storage")
    parser.add_argument("--simulation-case", default="accepted",
                        choices=("accepted", "timeout", "rejected"),
                        help="Offline simulation scenario; used with --simulate-checkout")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.list_ksle6_disks:
            from purchase_guard import run_list_disk_preferences
            return run_list_disk_preferences()
        if args.purchase_quota_status:
            from purchase_quota import run_quota_status
            return run_quota_status()
        if args.list_eco_options:
            from eco_discovery import run_list_eco_options
            return run_list_eco_options()
        if args.check_payment_readiness:
            from payment_readiness import run_payment_readiness
            return run_payment_readiness()
        if args.reconcile_account:
            from account_reconciliation import run_account_reconciliation
            return run_account_reconciliation()
        if args.simulate_checkout:
            from checkout_simulator import run_checkout_simulation
            return run_checkout_simulation(args.simulation_case)
        if args.reconcile_order_status:
            from checkout_simulator import run_order_reconciliation
            return run_order_reconciliation()
        if args.purchase_preview:
            from purchase_guard import run_purchase_preview
            return run_purchase_preview()
        if args.purchase_preflight:
            from purchase_preflight import run_purchase_preflight
            return run_purchase_preflight()
        if args.list_test_stock:
            from order_probe import TEST_PLANS
            plan_code = TEST_PLANS[args.test_plan]
            for stock in retrieve_stock(plan_code=plan_code):
                print(f"{stock.datacenter.upper():<6} {stock.status:<12} {stock.fqn}")
            return 0
        if args.test_order:
            from order_probe import run_test_order
            return run_test_order(args.datacenter, args.storage, args.test_plan, args.test_fqn)
        if args.diagnose_prices:
            from pricing import fetch_catalog, diagnostic_summary
            print(json.dumps(diagnostic_summary(fetch_catalog()), indent=2))
            return 0
        if args.list_prices:
            from pricing import cached_prices
            prices = cached_prices(state_path().with_name("pricing-cache.json"))
            if prices:
                for storage, value in sorted(prices.items()):
                    print(f"{STORAGE_NAMES.get(storage, storage)}: {value}")
            else:
                print("Sem preços de catálogo verificados; consultar o checkout OVHcloud.")
            return 0
        if args.send_test_email:
            sample = Stock(
                fqn=f"{PLAN_CODE}.ram-128g-ddr4-ecc-2400.{PREFERRED_STORAGE}",
                storage=PREFERRED_STORAGE, datacenter="bhs", status="1H-high",
            )
            _, plain = alert_content([sample])
            send_mail(
                mail_config(), "[OVH STOCK] Test email (simulated stock)",
                "EMAIL DE TESTE - disponibilidade simulada.\n\n" + plain,
                html_body=alert_html([sample], test=True),
            )
            LOGGER.info("Test email accepted by SMTP server")
            return 0

        # Manual --list and --check are independent diagnostics; the timer's
        # default --once obeys a persistent cooldown across service restarts.
        cooldown = cooldown_path() if not (args.list or args.check) else None
        if cooldown is not None and api_cooldown_active(cooldown):
            return 0
        try:
            stocks = retrieve_stock(int(os.getenv("HTTP_TIMEOUT", "15")))
        except APITransientError as exc:
            if cooldown is not None:
                apply_api_failure(cooldown, exc)
                return 0
            raise
        if cooldown is not None:
            until, failures = read_cooldown(cooldown)
            if until or failures:
                write_cooldown(cooldown, 0, 0)
        if args.list:
            print_stock(stocks)
            return 0
        config = None if args.check else mail_config()
        # Only consult cached catalog pricing when a new stock alert is pending.
        # A failed price lookup must never suppress or delay the stock email.
        prices = None
        if not args.check and config is not None:
            pending, _ = transition(monitored_stocks(stocks), load_alerted(state_path()))
            if pending:
                from pricing import cached_prices
                prices = cached_prices(state_path().with_name("pricing-cache.json"))
        return run_once(stocks, state_path(), config, dry_run=args.check, prices=prices)
    except (MonitorError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

