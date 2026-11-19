"""Global two-server KS-LE-6 quota: identical or different disks, Europe/Canada.

No live checkout transport exists in this application. A future single-host
purchaser must atomically reserve one of TWO global slots before any billable
request. The slots cannot be reset automatically. A timeout/uncertain order
blocks the second attempt until manually reconciled. Any legacy ledger must
be manually reconciled; never silently migrate or discard it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile

from purchase_guard import (
    APPROVED_FQNS, DATACENTERS, EXPECTED_FQN, PLAN_CODE,
    PurchaseSafetyError, parse_eur,
)

GLOBAL_LIMIT = 2
SCHEMA = 2
DEFAULT_PATH = "/var/lib/ovh-ksle-purchase/purchases-v2.json"
OPEN_STATES = frozenset(("reserved", "submitted", "unknown"))
STATES = OPEN_STATES | {"confirmed"}


def _valid_fqn(fqn: str) -> bool:
    return isinstance(fqn, str) and fqn in APPROVED_FQNS


class PurchaseQuota:
    """Atomic, persistent global quota, allowing repeated FQN and plan.

    Multiple purchasing hosts must NOT use this local filesystem lock.
    The caller receives an integer slot 1 or 2 (not a plan identifier).
    No live checkout method is provided here.
    """
    def __init__(self, path: Path, approved_fqns: tuple[str, ...] = (EXPECTED_FQN,),
                 max_servers: int = GLOBAL_LIMIT):
        if type(max_servers) is not int or max_servers not in (1, 2):
            raise PurchaseSafetyError("Global max server count must be 1 or 2")
        if (not isinstance(approved_fqns, tuple) or not approved_fqns
                or approved_fqns[0] != EXPECTED_FQN
                or len(set(approved_fqns)) != len(approved_fqns)
                or any(not _valid_fqn(f) for f in approved_fqns)):
            raise PurchaseSafetyError(
                "Only explicitly approved KS-LE-6 128GB disk FQNs permitted"
            )
        self.path = Path(path)
        self.approved = frozenset(approved_fqns)
        self.max_servers = max_servers

    def _check_legacy(self) -> None:
        """Never ignore existing entries from an older ledger."""
        for legacy_name in ("intent.json", "purchases-v1.json"):
            path = self.path.with_name(legacy_name)
            if path != self.path and (path.exists() or path.is_symlink()):
                raise PurchaseSafetyError(
                    f"Existing {legacy_name} requires reconciliation before new reservations"
                )

    def read(self) -> list[dict]:
        if self.path.is_symlink():
            raise PurchaseSafetyError("Purchase quota journal symlink forbidden")
        try:
            info = self.path.stat()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise PurchaseSafetyError("Cannot inspect purchase quota journal") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise PurchaseSafetyError("Purchase quota journal must be a 0600 regular file")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PurchaseSafetyError("Corrupted purchase quota journal; further orders blocked") from exc
        if (not isinstance(payload, dict) or payload.get("schema") != SCHEMA
                or not isinstance(payload.get("attempts"), list)
                or len(payload["attempts"]) > GLOBAL_LIMIT):
            raise PurchaseSafetyError("Unrecognized purchase quota schema or record count")
        attempts = payload["attempts"]
        slots: set[int] = set()
        for item in attempts:
            if not isinstance(item, dict):
                raise PurchaseSafetyError("Invalid purchase quota entry")
            slot = item.get("slot")
            amount = item.get("totalEur")
            if (type(slot) is not int or slot not in (1, 2) or slot in slots
                    or item.get("planCode") != PLAN_CODE
                    or not _valid_fqn(item.get("fqn"))
                    or item.get("datacenter") not in DATACENTERS
                    or item.get("status") not in STATES):
                raise PurchaseSafetyError("Invalid or duplicated purchase slot")
            try:
                parse_eur(amount, label="recorded initial charge")
            except PurchaseSafetyError as exc:
                raise PurchaseSafetyError("Invalid purchase quota initial charge") from exc
            if item["status"] in ("submitted", "confirmed"):
                if type(item.get("orderId")) is not int or not 0 < item["orderId"] < 10**15:
                    raise PurchaseSafetyError("Submitted record requires known order ID")
            elif "orderId" in item:
                raise PurchaseSafetyError("Unsubmitted or uncertain record has unexpected order ID")
            slots.add(slot)
        if sorted(slots) != list(range(1, len(attempts) + 1)):
            raise PurchaseSafetyError("Nonconsecutive purchase slots; refusing further purchases")
        return attempts

    def _write(self, attempts: list[dict]) -> None:
        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=self.path.parent,
                                             encoding="utf-8", prefix=".quota-",
                                             delete=False) as file:
                tmp = Path(file.name)
                os.fchmod(file.fileno(), 0o600)
                json.dump({"schema": SCHEMA, "attempts": attempts}, file, sort_keys=True)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self.path)
            tmp = None
            dirfd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        finally:
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _locked(self, callback):
        directory = self.path.parent
        if directory.is_symlink():
            raise PurchaseSafetyError("Purchase quota directory symlink forbidden")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.stat().st_mode & 0o022:
            raise PurchaseSafetyError("Purchase quota directory permissions unsafe")
        fd = os.open(str(directory / ".purchases-v2.lock"),
                     os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise PurchaseSafetyError("Unsafe purchase quota lock file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._check_legacy()
            return callback(self.read())
        finally:
            os.close(fd)

    def reserve(self, *, fqn: str, datacenter: str, total: Decimal) -> int | None:
        """Reserve one of two global slots; return slot number or None if full.

        A second identical KS-LE-6 configuration is allowed after the
        first order is confirmed. NEVER release a reserved slot on timeouts.
        """
        if not _valid_fqn(fqn) or fqn not in self.approved:
            raise PurchaseSafetyError("KS-LE-6 128GB disk FQN is not approved")
        if datacenter not in DATACENTERS:
            raise PurchaseSafetyError("Only allowed Europe/Canada datacenters may be ordered")
        if not isinstance(total, Decimal) or not total.is_finite() or total <= 0:
            raise PurchaseSafetyError("Initial EUR total must be a positive Decimal")
        total = parse_eur(total, label="initial EUR total")

        def apply(attempts: list[dict]) -> int | None:
            if len(attempts) >= self.max_servers:
                return None
            if any(item["status"] in OPEN_STATES for item in attempts):
                raise PurchaseSafetyError(
                    "Previous purchase is unresolved; cannot reserve another server"
                )
            slot = len(attempts) + 1
            attempts.append({
                "slot": slot,
                "planCode": PLAN_CODE,
                "fqn": fqn,
                "datacenter": datacenter,
                "totalEur": str(total),
                "status": "reserved",
                "createdAt": datetime.now(timezone.utc).isoformat(),
            })
            self._write(attempts)
            return slot

        return self._locked(apply)

    def update_outcome(self, *, slot: int, status: str,
                       order_id: int | None = None) -> None:
        """Only reserved->submitted/unknown, submitted->confirmed.

        'confirmed' requires external human verification: an order ID by
        itself is not evidence of payment or provisioning.
        """
        if type(slot) is not int or slot not in (1, 2):
            raise PurchaseSafetyError("Invalid purchase slot")
        if status not in ("submitted", "unknown", "confirmed"):
            raise PurchaseSafetyError("Invalid purchase outcome")
        if status in ("submitted", "confirmed"):
            if type(order_id) is not int or not 0 < order_id < 10**15:
                raise PurchaseSafetyError("Verified numeric order ID required")
        elif order_id is not None:
            raise PurchaseSafetyError("Unknown outcome cannot include an order ID")

        def apply(attempts: list[dict]) -> None:
            match = [record for record in attempts if record["slot"] == slot]
            if len(match) != 1:
                raise PurchaseSafetyError("Purchase slot not found")
            record = match[0]
            previous = record["status"]
            if (previous == "reserved" and status not in ("submitted", "unknown")) or (
                previous == "submitted" and status != "confirmed"
            ) or previous in ("unknown", "confirmed"):
                raise PurchaseSafetyError("Unsafe purchase outcome transition")
            if status == "confirmed" and record.get("orderId") != order_id:
                raise PurchaseSafetyError("Order ID differs from existing submitted order")
            record["status"] = status
            record["updatedAt"] = datetime.now(timezone.utc).isoformat()
            if order_id is not None:
                record["orderId"] = order_id
            self._write(attempts)

        self._locked(apply)


def run_quota_status() -> int:
    """Show local reservation count; does not create orders or query account."""
    try:
        configured = os.getenv("OVH_AUTO_BUY_MAX_SERVERS", "2")
        if configured not in ("1", "2"):
            raise PurchaseSafetyError("OVH_AUTO_BUY_MAX_SERVERS must be 1 or 2")
        path = Path(os.getenv("OVH_AUTO_BUY_QUOTA_JOURNAL", DEFAULT_PATH))
        quota = PurchaseQuota(path, max_servers=int(configured))
        quota._check_legacy()
        attempts = quota.read()
        print(f"KS-LE-6 purchase quota: {len(attempts)}/{quota.max_servers} "
              "reserved slots (local monitor ledger, not account-wide).")
        for item in attempts:
            print(
                f"  Slot {item['slot']}: {item['fqn']} / "
                f"{item['datacenter'].upper()} / {item['status']}"
            )
        print("Preview disk choices (not all authorized for purchase): " + ", ".join(
            fqn.split(".")[2] for fqn in APPROVED_FQNS
        ))
        print("Purchase approval currently limited to the original hybrid disk FQN.")
        print("Same or different disks can fill both slots once explicitly approved.")
        print("Only Europe or Canada; checkout DISABLED. No reservation created.")
        return 0
    except (PurchaseSafetyError, OSError, ValueError) as exc:
        print(f"Quota status unavailable; purchases blocked: {exc}")
        return 1
