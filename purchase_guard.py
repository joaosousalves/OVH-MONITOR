"""Disabled-by-default KS-LE-6 purchase readiness preview.

A purchasing endpoint is intentionally NOT implemented. It is impossible to
submit checkout through this module or the safe OVHCartClient allowlist.

The separate OrderJournal is a fail-closed durable reservation primitive for
a future, explicitly authorized checkout implementation. Preview reads this
journal but never reserves a purchase or writes it.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Mapping, Any

from ksle_monitor import (
    APITransientError, MonitorError, Stock, retrieve_stock, PLAN_CODE,
    PREFERRED_STORAGE,
)
from order_probe import (
    OrderProbeError, OVHCartClient, test_order, verify_available_test_fqn,
)

LOG = logging.getLogger("ksle-purchase-preview")
# These four KS-LE-6 layouts have 128 GB RAM; order is purchase priority.
# The default primary preference is unchanged and all alternatives are
# preview candidates ONLY until a separate live checkout is approved.
RAM_CODE = "ram-128g-ddr4-ecc-2400"
STORAGE_PRIORITIES = (
    "hybridsoftraid-2x4000sa-2x500nvme",  # 2 x 512 GB NVMe + 2 x 4 TB HDD
    "softraid-2x1920nvme",                # 2 x 1.92 TB NVMe
    "softraid-3x1000nvme",                # 3 x 1 TB NVMe
    "softraid-4x960ssd",                  # 4 x 960 GB SATA SSD
)
APPROVED_FQNS = tuple(f"{PLAN_CODE}.{RAM_CODE}.{storage}"
                      for storage in STORAGE_PRIORITIES)
EXPECTED_FQN = APPROVED_FQNS[0]  # Backward-compatible original preference.
# BHS Canada; FRA/GRA/RBX/SBG France; LON UK; WAW Poland.
DATACENTERS = ("bhs", "fra", "gra", "lon", "rbx", "sbg", "waw")
DEFAULT_JOURNAL = "/var/lib/ovh-ksle-purchase/intent.json"
EUR = Decimal("0.01")


class PurchaseSafetyError(Exception):
    """A missing guarantee, unexpected quote, unsafe state, or unmet policy."""


def parse_eur(value: Any, *, label: str, allow_zero: bool = False) -> Decimal:
    """Strict nonnegative EUR decimal, exactly representable to cents."""
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int, float, Decimal)):
        raise PurchaseSafetyError(f"{label} must be a numeric EUR amount")
    string = str(value)
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,2})?", string):
        raise PurchaseSafetyError(f"{label} must be a decimal EUR amount with at most 2 decimals")
    try:
        amount = Decimal(string)
    except InvalidOperation as exc:
        raise PurchaseSafetyError(f"{label} is not numeric") from exc
    if amount < 0 or (amount == 0 and not allow_zero):
        raise PurchaseSafetyError(f"{label} must be greater than zero")
    return amount.quantize(EUR)


def checkout_total_eur(quote: Any) -> Decimal:
    """Use OVH's authoritative first checkout total inclusive of VAT/fees.

    Reject prices without a structured numeric value and EUR currency. The
    initial charge is NOT a verified monthly-renewal amount.
    """
    if not isinstance(quote, dict):
        raise PurchaseSafetyError("Checkout quote is not a JSON object")
    prices = quote.get("prices")
    if not isinstance(prices, dict):
        raise PurchaseSafetyError("Checkout has no structured prices")
    gross = prices.get("withTax")
    if not isinstance(gross, dict) or gross.get("currencyCode") != "EUR":
        raise PurchaseSafetyError("Checkout gross total currency is not explicitly EUR")
    return parse_eur(gross.get("value"), label="checkout total incl. VAT")


@dataclass(frozen=True)
class PurchasePolicy:
    enabled: bool
    max_total_eur: Decimal
    datacenters: tuple[str, ...]
    max_servers: int = 2

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "PurchasePolicy":
        env = os.environ if env is None else env
        raw_enabled = env.get("OVH_AUTO_BUY_ENABLED", "false").strip().lower()
        if raw_enabled not in ("false", "true", "0", "1"):
            raise PurchaseSafetyError("OVH_AUTO_BUY_ENABLED must be true or false")
        enabled = raw_enabled in ("true", "1")
        # Explicitly reject activation even if a user manually edits the env.
        # This phase NEVER creates a paid order.
        if enabled:
            raise PurchaseSafetyError(
                "Checkout is NOT implemented or authorized. "
                "Set OVH_AUTO_BUY_ENABLED=false; preview only."
            )
        raw_limit = env.get("OVH_AUTO_BUY_MAX_TOTAL_EUR", "").strip()
        if not raw_limit:
            raise PurchaseSafetyError(
                "Set OVH_AUTO_BUY_MAX_TOTAL_EUR explicitly in /etc/ovh-ksle-order.env"
            )
        max_servers_raw = env.get("OVH_AUTO_BUY_MAX_SERVERS", "2").strip()
        if max_servers_raw not in ("1", "2"):
            raise PurchaseSafetyError(
                "OVH_AUTO_BUY_MAX_SERVERS must be 1 or 2; a third server is forbidden"
            )
        max_servers = int(max_servers_raw)
        limit = parse_eur(raw_limit, label="OVH_AUTO_BUY_MAX_TOTAL_EUR")
        if limit > Decimal("500.00"):
            raise PurchaseSafetyError("Safety ceiling: max allowed initial total is 500.00 EUR")
        dc_values = env.get("OVH_AUTO_BUY_ALLOWED_DATACENTERS", ",".join(DATACENTERS))
        allowed = [value.strip().lower() for value in dc_values.split(",")]
        if not allowed or any(value not in DATACENTERS for value in allowed):
            raise PurchaseSafetyError("Invalid OVH_AUTO_BUY_ALLOWED_DATACENTERS")
        if len(set(allowed)) != len(allowed):
            raise PurchaseSafetyError("Duplicate datacenter in OVH_AUTO_BUY_ALLOWED_DATACENTERS")
        return cls(enabled=False, max_total_eur=limit, datacenters=tuple(allowed),
                   max_servers=max_servers)


def choose_candidate(stocks: list[Stock], policy: PurchasePolicy) -> Stock | None:
    """Rank ONLY KS-LE-6 128GB variants; disk priority before datacenter.

    The stock feed's FQN and separate storage field must both agree.
    Unrecognized hardware, datacenters and availability codes never qualify.
    """
    candidates = [
        stock for stock in stocks
        if stock.fqn in APPROVED_FQNS
        and stock.storage in STORAGE_PRIORITIES
        and stock.fqn == f"{PLAN_CODE}.{RAM_CODE}.{stock.storage}"
        and stock.available
        and stock.datacenter in policy.datacenters
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda stock: (
            STORAGE_PRIORITIES.index(stock.storage),
            policy.datacenters.index(stock.datacenter),
        ),
    )


class OrderJournal:
    """One-purchase gate: persists before any future potentially billable call.

    If an attempted order has an unknown outcome, it stays blocked until an
    operator reconciles the actual order in OVHcloud. No automatic reset.
    The current preview NEVER calls reserve_once().
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> dict | None:
        if self.path.is_symlink():
            raise PurchaseSafetyError("Journal symlink forbidden")
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PurchaseSafetyError("Cannot inspect purchase journal") from exc
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
            raise PurchaseSafetyError("Purchase journal must be a private regular file (0600)")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PurchaseSafetyError("Purchase journal unreadable; refusing any retry") from exc
        if (not isinstance(data, dict) or data.get("schema") != 1
                or data.get("planCode") != PLAN_CODE
                or data.get("status") not in ("reserved", "submitted", "unknown")):
            raise PurchaseSafetyError("Invalid purchase journal; refusing any retry")
        return data

    def reserve_once(self, *, fqn: str, datacenter: str, total: Decimal) -> bool:
        """Future checkout only: atomic permanent reservation, no auto-retry."""
        if fqn != EXPECTED_FQN or datacenter not in DATACENTERS:
            raise PurchaseSafetyError("Cannot reserve anything except exact KS-LE-6 preference")
        if not isinstance(total, Decimal) or not total.is_finite() or total <= 0:
            raise PurchaseSafetyError("Invalid price for purchase reservation")
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_stat = directory.stat()
        if directory_stat.st_mode & 0o022:
            raise PurchaseSafetyError("Purchase journal parent must not be group/world writable")
        lock_path = directory / ".intent.lock"
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        tmp = None
        try:
            lock_stat = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_mode & 0o077:
                raise PurchaseSafetyError("Purchase lock permissions are unsafe")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if self.read() is not None:
                return False
            record = {
                "schema": 1,
                "planCode": PLAN_CODE,
                "fqn": fqn,
                "datacenter": datacenter,
                "totalEur": str(total),
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "status": "reserved",
            }
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                             prefix=".order-", delete=False) as handle:
                tmp = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                json.dump(record, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            tmp = None
            dirfd = os.open(str(directory), os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
            return True
        finally:
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            os.close(lock_fd)


    def record_outcome(self, status: str, *, order_id: int | None = None) -> None:
        """Persist result without ever clearing the duplicate-purchase guard.

        'submitted' means that an order identifier was returned; it does NOT
        prove payment or provisioning. 'unknown' demands manual reconciliation.
        A receipt/timeout must never trigger a second checkout attempt.
        """
        if status not in ("submitted", "unknown"):
            raise PurchaseSafetyError("Invalid order outcome")
        if status == "submitted" and (
                type(order_id) is not int or not 0 < order_id < 10**15):
            raise PurchaseSafetyError("Valid numeric order ID required")
        if status == "unknown" and order_id is not None:
            raise PurchaseSafetyError("Do not attach order ID to unknown status")
        directory = self.path.parent
        if not directory.is_dir():
            raise PurchaseSafetyError("No existing purchase journal directory")
        if directory.stat().st_mode & 0o022:
            raise PurchaseSafetyError("Purchase journal directory permissions unsafe")
        lock_path = directory / ".intent.lock"
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR
                          | getattr(os, "O_NOFOLLOW", 0), 0o600)
        tmp = None
        try:
            lock_stat = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_mode & 0o077:
                raise PurchaseSafetyError("Purchase lock permissions unsafe")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            record = self.read()
            if record is None or record.get("status") != "reserved":
                raise PurchaseSafetyError("Only reserved purchases may change outcome")
            record["status"] = status
            record["updatedAt"] = datetime.now(timezone.utc).isoformat()
            if order_id is not None:
                record["orderId"] = order_id
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                             prefix=".outcome-", delete=False) as handle:
                tmp = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                json.dump(record, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            tmp = None
            directory_fd = os.open(str(directory), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            os.close(lock_fd)




def make_api_client(env: Mapping[str, str] | None = None) -> OVHCartClient:
    env = os.environ if env is None else env
    return OVHCartClient(
        env.get("OVH_TOKEN", "").strip(),
        application_key=env.get("OVH_APPLICATION_KEY", "").strip(),
        application_secret=env.get("OVH_APPLICATION_SECRET", "").strip(),
        consumer_key=env.get("OVH_CONSUMER_KEY", "").strip(),
    )


def preview(policy: PurchasePolicy, *, stocks: list[Stock],
            client: OVHCartClient | None, journal: OrderJournal,
            quota: Any = None) -> str:
    """Read-only decision, plus optional safe temporary cart and quote.

    An empty stock list fails closed to avoid treating a partial feed as
    evidence of unavailable stock. On successful stock matching, recheck the
    exact FQN before preparing a cart.
    """
    if policy.enabled:
        raise PurchaseSafetyError("Checkout is disabled in this release")
    if journal.read() is not None:
        raise PurchaseSafetyError(
            "An earlier purchase was reserved/submitted or is uncertain. "
            "Reconcile it in the OVHcloud account; preview refuses further carts."
        )
    if quota is not None:
        from purchase_quota import OPEN_STATES
        quota._check_legacy()
        attempts = quota.read()
        if len(attempts) >= policy.max_servers:
            return f"LIMIT REACHED: {len(attempts)}/{policy.max_servers} servers reserved. No cart created."
        if any(record["status"] in OPEN_STATES for record in attempts):
            raise PurchaseSafetyError(
                "Previous KS-LE-6 purchase not yet reconciled; no new cart created"
            )
    if not stocks:
        raise PurchaseSafetyError("Stock feed returned no rows")
    choice = choose_candidate(stocks, policy)
    if choice is None:
        return "WAITING: no eligible KS-LE-6 128 GB disk configuration in approved EU/CA datacenters. No cart created."
    if client is None:
        raise PurchaseSafetyError("Authenticated cart credentials required to quote available stock")
    verify_available_test_fqn(PLAN_CODE, choice.fqn, choice.datacenter)
    holder: dict[str, Decimal] = {}
    def validate(quote_data: Any) -> None:
        total = checkout_total_eur(quote_data)
        if total > policy.max_total_eur:
            raise PurchaseSafetyError(
                f"PRICE BLOCKED: {total:.2f} EUR incl. VAT exceeds cap "
                f"{policy.max_total_eur:.2f} EUR"
            )
        holder["total"] = total
    test_order(client, datacenter=choice.datacenter, plan_code=PLAN_CODE,
               target_fqn=choice.fqn, quote_validator=validate)
    amount = holder.get("total")
    if amount is None:
        raise PurchaseSafetyError("No validated checkout price")
    return (
        f"PREVIEW ONLY: eligible {PLAN_CODE}, {choice.datacenter.upper()}, "
        f"{choice.fqn}; initial total {amount:.2f} EUR incl. VAT "
        f"<= cap {policy.max_total_eur:.2f} EUR. "
        "No order/payment submitted. Recurring renewal charge not independently verified."
    )


def run_purchase_preview() -> int:
    try:
        policy = PurchasePolicy.from_env()
        journal_path = Path(os.getenv("OVH_AUTO_BUY_JOURNAL", DEFAULT_JOURNAL))
        journal = OrderJournal(journal_path)
        from purchase_quota import PurchaseQuota, DEFAULT_PATH
        quota = PurchaseQuota(
            Path(os.getenv("OVH_AUTO_BUY_QUOTA_JOURNAL", DEFAULT_PATH)),
            max_servers=policy.max_servers,
        )
        # Query stock before creating credentials-based client. No stock =>
        # no account API calls, no cart and no journal changes.
        stocks = retrieve_stock()
        candidate = choose_candidate(stocks, policy)
        api = make_api_client() if candidate is not None else None
        result = preview(policy, stocks=stocks, client=api, journal=journal, quota=quota)
        print(result)
        return 0
    except (PurchaseSafetyError, OrderProbeError, MonitorError, APITransientError) as exc:
        LOG.error("Purchase preview blocked (no order made): %s", exc)
        return 1


def render_disk_preferences(stocks: list[Stock]) -> str:
    """Human-readable 128GB KS-LE-6 storage priority and live stock report."""
    if not stocks:
        raise PurchaseSafetyError("No KS-LE-6 stock records; availability unknown")
    headings = {
        STORAGE_PRIORITIES[0]: "2 x 512 GB NVMe + 2 x 4 TB HDD (existing first choice)",
        STORAGE_PRIORITIES[1]: "2 x 1.92 TB NVMe",
        STORAGE_PRIORITIES[2]: "3 x 1 TB NVMe",
        STORAGE_PRIORITIES[3]: "4 x 960 GB SATA SSD",
    }
    lines = [
        "KS-LE-6 / 128 GB RAM: disk preferences, highest priority first",
        "Allowed countries: Canada (BHS), France, UK, Poland (European datacenters)",
        "Two KS-LE-6 servers maximum, same or different disks; checkout disabled.",
    ]
    for number, storage in enumerate(STORAGE_PRIORITIES, 1):
        matches = sorted(
            (item for item in stocks
             if item.storage == storage
             and item.fqn == f"{PLAN_CODE}.{RAM_CODE}.{storage}"
             and item.datacenter in DATACENTERS),
            key=lambda item: DATACENTERS.index(item.datacenter),
        )
        lines.append(f"\n{number}. {headings[storage]}")
        if not matches:
            lines.append("   Not reported in the current stock feed (UNKNOWN, not sold out).")
        for item in matches:
            lines.append(f"   {item.datacenter.upper():<4} {item.status}")
    return "\n".join(lines)


def run_list_disk_preferences() -> int:
    """Public read-only; no cart, API credentials or purchase journal."""
    try:
        print(render_disk_preferences(retrieve_stock(plan_code=PLAN_CODE)))
        return 0
    except (PurchaseSafetyError, MonitorError, APITransientError) as exc:
        LOG.error("Disk inventory not confirmed: %s", exc)
        return 1
