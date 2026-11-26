"""Opt-in, systemd-timed commercial audit with persistent email deduplication.

This watcher is separate from the existing 30-second stock alert timer.
It NEVER submits an order, charges payment, or reserves a server. A temporary
cart is only created by commercial_audit when stock is newly observed.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Any, Callable

from account_reconciliation import AccountReconciliationError
from commercial_audit import audit_commercial
from health_monitor import HealthError, record_heartbeat
from ksle_monitor import (
    APITransientError, MonitorError, Stock, PRODUCT_URL,
    api_cooldown_active, apply_api_failure, eligible_alert_stock,
    mail_config, read_cooldown,
    retrieve_stock, send_mail, write_cooldown,
)
from order_probe import OrderProbeError
from retained_cart import ensure_no_active_cart, persist_retained
from commitment_guard import CommitmentSafetyError
from pending_orders import PendingOrdersError
from purchase_guard import (
    DEFAULT_JOURNAL, OrderJournal, PurchasePolicy, PurchaseSafetyError,
    choose_candidate, make_api_client,
)
from purchase_quota import DEFAULT_PATH, PurchaseQuota


LOG = logging.getLogger("ksle-commercial-watch")
DEFAULT_WATCH_PATH = "/var/lib/ovh-ksle-purchase/commercial-watch.json"
MIN_RETRY_SECONDS = 900       # A failed commercial audit never loops rapidly.
MAX_RETRY_SECONDS = 3600
MAX_REPORT_CHARS = 8000


class WatchStateError(Exception):
    """Unsafe/corrupted state; refuse to create any temporary cart."""


def _blank_state() -> dict[str, Any]:
    return {"schema": 1, "seen": [], "attempt": None, "last_attempt_at": 0.0}


def _valid_key(key: Any) -> bool:
    return isinstance(key, str) and 0 < len(key) < 300 and "@" in key


def read_state(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise WatchStateError("Commercial watch state cannot be a symlink")
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return _blank_state()
    except OSError as exc:
        raise WatchStateError("Unable to read commercial watch state metadata") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise WatchStateError("Commercial watch state must be a private 0600 file")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WatchStateError("Commercial watch state unreadable; refusing retries") from exc
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise WatchStateError("Unexpected commercial watch state schema")
    seen, attempted, last = raw.get("seen"), raw.get("attempt"), raw.get("last_attempt_at")
    if (not isinstance(seen, list) or len(seen) > 40 or
            any(not _valid_key(key) for key in seen) or len(set(seen)) != len(seen)):
        raise WatchStateError("Invalid available-stock history")
    if type(last) not in (int, float) or not 0 <= last < 10**13:
        raise WatchStateError("Invalid commercial watch timestamp")
    if attempted is not None:
        if not isinstance(attempted, dict) or not _valid_key(attempted.get("key")):
            raise WatchStateError("Invalid commercial watch attempt")
        if attempted.get("status") not in ("started", "failed", "complete", "sending"):
            raise WatchStateError("Invalid commercial watch attempt state")
        if (type(attempted.get("failures")) is not int or
                not 0 <= attempted["failures"] <= 100):
            raise WatchStateError("Invalid commercial watch failure count")
        if attempted.get("status") in ("complete", "sending"):
            if (not isinstance(attempted.get("report"), str) or
                    not 0 < len(attempted["report"]) <= MAX_REPORT_CHARS or
                    type(attempted.get("emailed")) is not bool):
                raise WatchStateError("Malformed pending commercial alert")
    return raw


def save_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = path.parent.stat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_mode & 0o077:
        raise WatchStateError("Commercial watch state directory must be private (0700)")
    if path.is_symlink():
        raise WatchStateError("Commercial watch state cannot be a symlink")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=".commercial-watch-", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        temporary = None
    except OSError as exc:
        raise WatchStateError("Cannot persist commercial watch history") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def watch_once(policy: PurchasePolicy, *, stocks: list[Stock], path: Path,
               audit: Callable[[], str], notify: Callable[[Stock, str], None],
               now: float | None = None, fast_new_edge: bool = False) -> str:
    """Audit once on an observed availability edge, with durable retry gates.

    Persists an attempt *before* invoking cart code so a crash cannot cause
    5-minute cart loops. Saves the report before SMTP; failed email delivery
    retries the same report without touching the account/cart APIs again.
    """
    if policy.enabled:
        raise PurchaseSafetyError("Checkout activation is forbidden")
    now = time.time() if now is None else now
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise WatchStateError("Commercial watch directory must be private")
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        lock_metadata = os.fstat(fd)
        if not stat.S_ISREG(lock_metadata.st_mode) or lock_metadata.st_mode & 0o077:
            raise WatchStateError("Unsafe commercial watch lock file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        state = read_state(path)
        available = sorted(
            stock.key for stock in stocks
            if eligible_alert_stock(stock) and stock.available
            and stock.datacenter in policy.datacenters
        )
        candidate = choose_candidate(stocks, policy)
        previous = set(state["seen"])
        if (state["attempt"] is not None and
                state["attempt"]["status"] == "sending"):
            # An SMTP acknowledgement might have been lost. Keep this
            # durable uncertainty even when stock disappears; manual review
            # is required before any further commercial audit notification.
            if available != state["seen"]:
                state["seen"] = available
                save_state(path, state)
            return "REVIEW REQUIRED: previous email outcome uncertain; no duplicate sent."
        if candidate is None:
            if state["seen"] or state["attempt"] is not None:
                state["seen"] = []
                state["attempt"] = None
                save_state(path, state)
            return "WAITING: no eligible KS-LE-6 stock; no commercial cart or email."

        if candidate.key not in previous:
            # Rearm when a previously unavailable configuration reappears.
            state["attempt"] = None
        if available != state["seen"]:
            state["seen"] = available
            save_state(path, state)

        attempt = state["attempt"]
        if attempt is not None and attempt["key"] == candidate.key:
            if attempt["status"] == "sending":
                # The process might have crashed after SMTP accepted the
                # message. Never blindly resend an uncertain email.
                return "REVIEW REQUIRED: previous email outcome uncertain; no duplicate sent."
            if attempt["status"] == "complete":
                if attempt["emailed"]:
                    return "UNCHANGED: commercial audit already emailed for this stock event."
                attempt["status"] = "sending"
                save_state(path, state)
                notify(candidate, attempt["report"])
                attempt["status"] = "complete"
                attempt["emailed"] = True
                save_state(path, state)
                return "SENT: saved commercial audit email delivered; no new cart."
            failures = attempt["failures"]
            retry = min(MAX_RETRY_SECONDS, MIN_RETRY_SECONDS * (2 ** min(failures, 3)))
            if now - state["last_attempt_at"] < retry:
                return "COOLDOWN: previous commercial audit pending/failed; no new cart."
        elif (now - state["last_attempt_at"] < MIN_RETRY_SECONDS
              and not (fast_new_edge and candidate.key not in previous)):
            return "COOLDOWN: recent commercial audit; no new cart."

        failures = (attempt["failures"] + 1 if attempt is not None and
                    attempt["key"] == candidate.key and attempt["status"] == "failed"
                    else 0)
        state["attempt"] = {"key": candidate.key, "status": "started", "failures": failures}
        state["last_attempt_at"] = now
        save_state(path, state)
        try:
            report = audit()
            if not isinstance(report, str) or not report.startswith(("COMMERCIAL AUDIT PARTIAL", "LIVE CART PREPARED")):
                # Includes sudden quota saturation or disappearing stock;
                # never email these as an approved/completed quote.
                raise PurchaseSafetyError("No complete eligible commercial quote")
            if len(report) > MAX_REPORT_CHARS:
                raise PurchaseSafetyError("Commercial audit report exceeds bounded size")
        except (PurchaseSafetyError, PendingOrdersError, AccountReconciliationError,
                OrderProbeError, MonitorError, CommitmentSafetyError) as exc:
            state["attempt"] = {"key": candidate.key, "status": "failed",
                                "failures": failures}
            save_state(path, state)
            LOG.warning("Commercial audit blocked (no purchase): %s", exc)
            return "RETRY LATER: commercial audit incomplete; no order or payment."

        state["attempt"] = {"key": candidate.key, "status": "complete",
                            "failures": 0, "report": report, "emailed": False}
        save_state(path, state)
        state["attempt"]["status"] = "sending"
        save_state(path, state)
        notify(candidate, report)
        state["attempt"]["status"] = "complete"
        state["attempt"]["emailed"] = True
        save_state(path, state)
        return ("SENT: live checkout result emailed." if "\nLIVE CHECKOUT" in report
                else "SENT: commercial audit email delivered; checkout/payment disabled.")
    finally:
        os.close(fd)


def run_commercial_watch() -> int:
    """One-shot watcher for a separate, opt-in 5-minute systemd timer."""
    try:
        policy = PurchasePolicy.from_env()
        smtp = mail_config()
        watch_path = Path(os.getenv("OVH_COMMERCIAL_WATCH_STATE", DEFAULT_WATCH_PATH))
        from live_purchase import enabled as live_enabled, try_retained_checkout, prepare_live_cart
        live = live_enabled()
        LOG.info(
            "KS-LE-6 (27skle06-v1) 128GB watcher: mode=%s; max servers=%d; "
            "initial cap=%s EUR; monthly cap=%s EUR; contract SHA256 pin=%s",
            "LIVE_CHECKOUT" if live else "AUDIT_ONLY", policy.max_servers,
            policy.max_total_eur, policy.max_monthly_eur,
            "set" if os.getenv("OVH_LIVE_APPROVED_CONTRACT_SHA256", "").strip() else "unset",
        )
        retained_path = watch_path.with_name("retained-cart.json")
        quota = PurchaseQuota(
            Path(os.getenv("OVH_AUTO_BUY_QUOTA_JOURNAL", DEFAULT_PATH)),
            max_servers=policy.max_servers,
        )
        journal = OrderJournal(Path(os.getenv("OVH_AUTO_BUY_JOURNAL", DEFAULT_JOURNAL)))
        if live and (retained_path.exists() or retained_path.is_symlink() or quota.path.exists()):
            status = try_retained_checkout(policy, make_api_client(), journal, quota, retained_path)
            if status is not None:
                LOG.info(status)
                if status.startswith(("LIVE CHECKOUT SUBMITTED", "LIVE CHECKOUT UNKNOWN")):
                    send_mail(smtp, "[OVH] KS-LE-6 live checkout result", status)
                record_heartbeat(watch_path.with_name("commercial-heartbeat.json"))
                return 0
        stock_cooldown = watch_path.with_name("commercial-stock-backoff.json")
        if api_cooldown_active(stock_cooldown):
            return 0
        try:
            stocks = retrieve_stock()
        except APITransientError as exc:
            apply_api_failure(stock_cooldown, exc)
            return 0
        until, failures = read_cooldown(stock_cooldown)
        if until or failures:
            write_cooldown(stock_cooldown, 0, 0)

        def perform_audit() -> str:
            api = make_api_client()
            retained_path = watch_path.with_name("retained-cart.json")
            ensure_no_active_cart(retained_path, api)
            quota = PurchaseQuota(
                Path(os.getenv("OVH_AUTO_BUY_QUOTA_JOURNAL", DEFAULT_PATH)),
                max_servers=policy.max_servers,
            )
            journal = OrderJournal(Path(os.getenv("OVH_AUTO_BUY_JOURNAL", DEFAULT_JOURNAL)))
            if live:
                report = prepare_live_cart(policy, api, stocks, journal, quota, retained_path)
            else:
                report = audit_commercial(
                    policy, stocks=stocks, client=api, journal=journal, quota=quota,
                    cart_retainer=lambda **record: persist_retained(retained_path, **record),
                )
            if live and retained_path.exists():
                status = try_retained_checkout(policy, api, journal, quota, retained_path)
                if status is not None:
                    LOG.info(status)
                    report += "\n" + status
            return report

        def mail_result(candidate: Stock, report: str) -> None:
            send_mail(
                smtp,
                (f"[OVH LIVE] KS-LE-6 {candidate.datacenter.upper()} checkout status"
                 if live else f"[OVH AUDIT] KS-LE-6 {candidate.datacenter.upper()} (NO PURCHASE)"),
                report + "\n\n" + f"Ver na OVHcloud: {PRODUCT_URL}\n"
                + ("Consulte o resultado LIVE CHECKOUT; pagamento não confirmado."
                   if live else "Nenhuma encomenda foi submetida. Verifique condições comerciais manualmente."),
            )

        result = watch_once(
            policy, stocks=stocks, path=watch_path,
            audit=perform_audit, notify=mail_result, fast_new_edge=live,
        )
        if result.startswith(("WAITING:", "UNCHANGED:", "SENT:")):
            record_heartbeat(watch_path.with_name("commercial-heartbeat.json"))
        LOG.info(result)
        return 0
    except (WatchStateError, HealthError, PurchaseSafetyError, MonitorError, OrderProbeError,
            PendingOrdersError, AccountReconciliationError,
            ValueError, TypeError, OSError) as exc:
        LOG.error("Commercial watcher stopped safely (no purchase): %s", exc)
        return 1


def run_commercial_watch_status() -> int:
    """Inspect watcher journal without API, SMTP, or changes to the state."""
    try:
        path = Path(os.getenv("OVH_COMMERCIAL_WATCH_STATE", DEFAULT_WATCH_PATH))
        state = read_state(path)
        attempt = state["attempt"]
        if attempt is None:
            print(f"COMMERCIAL WATCH: {len(state['seen'])} stock key(s) seen; no audit attempted.")
        else:
            print(
                f"COMMERCIAL WATCH: {len(state['seen'])} stock key(s) seen; "
                f"audit status={attempt['status']}; "
                f"email={'confirmed' if attempt.get('emailed') else 'not confirmed'}; "
                "checkout/payment disabled."
            )
            if attempt["status"] == "sending":
                print(
                    "REVIEW REQUIRED: SMTP outcome uncertain; verify your inbox/logs. "
                    "The watcher will not resend automatically or delete its state."
                )
        return 0
    except (WatchStateError, OSError) as exc:
        LOG.error("Cannot inspect commercial watch state: %s", exc)
        return 1
