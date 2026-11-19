"""Two-server global purchase quota. NO live checkout transport.

Future purchase workers must use a SINGLE shared ledger path for every
candidate plan and reserve before any potentially billable API call.
A reservation, uncertain outcome or returned order ID is never automatically
released. No live CLI calls reserve here. Current preferred allowlist contains
KS-LE-6 only; a second distinct model must be explicitly approved later.
"""
from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from purchase_guard import EXPECTED_FQN, PurchaseSafetyError

GLOBAL_LIMIT = 2
DEFAULT_PATH = "/var/lib/ovh-ksle-purchase/purchases-v1.json"
ALLOWED_DATACENTERS = {"bhs", "fra", "gra", "lon", "rbx", "sbg", "waw"}
KNOWN_PLANS = {f"27skle0{i}-v1" for i in range(1, 7)}
OPEN_STATES = {"reserved", "submitted", "unknown"}
ALL_STATES = OPEN_STATES | {"confirmed"}


def plan_from_fqn(fqn: str) -> str:
    if not isinstance(fqn, str) or len(fqn) > 240:
        raise PurchaseSafetyError("Invalid approved server FQN")
    parts = fqn.split(".")
    if (len(parts) != 3 or parts[0] not in KNOWN_PLANS
            or not parts[1].startswith("ram-")
            or not all(re.fullmatch(r"[a-z0-9-]+", p) for p in parts[1:])):
        raise PurchaseSafetyError("Server FQN is not a recognized KS-LE configuration")
    return parts[0]


class PurchaseQuota:
    """Atomic single-host reservation ledger, at most two distinct KS-LE plans.

    A worker MAY submit a single checkout only after obtaining a reservation.
    The worker must never retry automatically when outcome is not confirmed.
    A shared filesystem across multiple hosts requires distributed locking
    instead; don't run more than one purchaser host with this implementation.
    """

    def __init__(self, path: Path, approved_fqns: tuple[str, ...] = (EXPECTED_FQN,),
                 max_servers: int = GLOBAL_LIMIT):
        if type(max_servers) is not int or not 1 <= max_servers <= GLOBAL_LIMIT:
            raise PurchaseSafetyError("Maximum server purchases must be 1 or 2")
        if (not approved_fqns or len(approved_fqns) > GLOBAL_LIMIT
                or approved_fqns[0] != EXPECTED_FQN):
            raise PurchaseSafetyError("KS-LE-6 must be primary; one secondary model may be approved")
        plans = [plan_from_fqn(fqn) for fqn in approved_fqns]
        if len(plans) != len(set(plans)):
            raise PurchaseSafetyError("Only one server per distinct plan is allowed")
        self.path = Path(path)
        self.approved = frozenset(approved_fqns)
        self.max_servers = max_servers

    def read(self) -> list[dict]:
        if self.path.is_symlink():
            raise PurchaseSafetyError("Quota ledger may not be a symlink")
        try:
            info = self.path.stat()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise PurchaseSafetyError("Cannot inspect quota ledger") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise PurchaseSafetyError("Quota ledger must be a private regular file (0600)")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise PurchaseSafetyError("Corrupt quota ledger: refusing new purchase") from exc
        if not isinstance(data, dict) or data.get("schema") != 1:
            raise PurchaseSafetyError("Unrecognized quota ledger schema")
        attempts = data.get("attempts")
        if not isinstance(attempts, list) or len(attempts) > GLOBAL_LIMIT:
            raise PurchaseSafetyError("Invalid quota ledger entry count")
        plans: set[str] = set()
        for attempt in attempts:
            if not isinstance(attempt, dict):
                raise PurchaseSafetyError("Invalid purchase attempt")
            plan = plan_from_fqn(attempt.get("fqn"))
            if (plan != attempt.get("planCode") or plan in plans
                    or attempt.get("status") not in ALL_STATES
                    or attempt.get("datacenter") not in ALLOWED_DATACENTERS):
                raise PurchaseSafetyError("Invalid or duplicated quota record")
            plans.add(plan)
        return attempts

    def _write(self, records: list[dict]) -> None:
        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".quota-", delete=False) as handle:
                tmp = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                json.dump({"schema": 1, "attempts": records}, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            tmp = None
            directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
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

    def _with_lock(self, change):
        directory = self.path.parent
        if directory.is_symlink():
            raise PurchaseSafetyError("Quota directory may not be a symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.stat().st_mode & 0o022:
            raise PurchaseSafetyError("Quota directory must not be writable by group/others")
        lock_path = directory / ".purchases-v1.lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR |
                     getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise PurchaseSafetyError("Unsafe quota lock")
            fcntl.flock(fd, fcntl.LOCK_EX)
            records = self.read()
            return change(records)
        finally:
            os.close(fd)

    def reserve(self, *, fqn: str, datacenter: str, total: Decimal) -> bool:
        if fqn not in self.approved:
            raise PurchaseSafetyError("Server FQN is not explicitly approved for purchasing")
        if datacenter not in ALLOWED_DATACENTERS:
            raise PurchaseSafetyError("Invalid datacenter")
        if not isinstance(total, Decimal) or not total.is_finite() or total <= 0:
            raise PurchaseSafetyError("Invalid initial EUR charge")
        plan = plan_from_fqn(fqn)

        def apply(records):
            # Legacy one-purchase journal must be reconciled before migration;
            # never silently ignore historic or ambiguous order attempts.
            legacy_path = self.path.with_name("intent.json")
            if legacy_path.exists() or legacy_path.is_symlink():
                raise PurchaseSafetyError("Legacy order journal requires manual migration")
            if len(records) >= self.max_servers or any(
                    item["planCode"] == plan for item in records):
                return False
            if any(item["status"] in OPEN_STATES for item in records):
                raise PurchaseSafetyError("Previous order outcome requires reconciliation")
            records.append({
                "planCode": plan, "fqn": fqn, "datacenter": datacenter,
                "totalEur": str(total), "status": "reserved",
                "createdAt": datetime.now(timezone.utc).isoformat(),
            })
            self._write(records)
            return True

        return self._with_lock(apply)

    def update_outcome(self, *, plan_code: str, status: str,
                       order_id: int | None = None) -> None:
        """Test-ready transitions. Confirmed requires a known order ID.

        Automated outcome reconciliation cannot confirm payment from a status
        string alone. A future operator-verified workflow must drive confirmed.
        """
        if status not in ("submitted", "unknown", "confirmed"):
            raise PurchaseSafetyError("Unsupported quota outcome")
        if status in ("submitted", "confirmed") and (
                type(order_id) is not int or not 0 < order_id < 10**15):
            raise PurchaseSafetyError("Submitted/confirmed outcome requires numeric order ID")
        if status == "unknown" and order_id is not None:
            raise PurchaseSafetyError("Unknown outcome cannot have an order ID")

        def apply(records):
            found = [x for x in records if x["planCode"] == plan_code]
            if len(found) != 1:
                raise PurchaseSafetyError("No reserved quota record for selected plan")
            item = found[0]
            old = item["status"]
            if ((old == "reserved" and status not in ("submitted", "unknown"))
                    or (old == "submitted" and status != "confirmed")
                    or old in ("unknown", "confirmed")):
                raise PurchaseSafetyError("Unsafe purchase quota state transition")
            if status == "confirmed" and item.get("orderId") != order_id:
                raise PurchaseSafetyError("Confirmed order ID must match recorded submission")
            item["status"] = status
            item["updatedAt"] = datetime.now(timezone.utc).isoformat()
            if order_id is not None:
                item["orderId"] = order_id
            self._write(records)

        self._with_lock(apply)


def run_quota_status() -> int:
    """Read-only local quota report; not an OVHcloud account order query."""
    try:
        configured = os.getenv("OVH_AUTO_BUY_MAX_SERVERS", "2")
        if configured not in ("1", "2"):
            raise PurchaseSafetyError("OVH_AUTO_BUY_MAX_SERVERS must be 1 or 2")
        path = Path(os.getenv("OVH_AUTO_BUY_QUOTA_JOURNAL", DEFAULT_PATH))
        quota = PurchaseQuota(path, max_servers=int(configured))
        legacy = path.with_name("intent.json")
        if legacy.exists() or legacy.is_symlink():
            raise PurchaseSafetyError(
                "Legacy intent journal found: migrate/reconcile before enabling purchases"
            )
        attempts = quota.read()
        print(f"Local automated purchase quota: {len(attempts)}/{quota.max_servers} "
              "reservations used (not an OVHcloud account-wide total).")
        for item in attempts:
            print(f"  {item['planCode']} {item['datacenter'].upper()}: {item['status']}")
        print("Approved for future preview: KS-LE-6 exact preferred FQN only.")
        print("Secondary model NOT YET APPROVED. Automatic checkout is DISABLED.")
        return 0
    except (PurchaseSafetyError, OSError, ValueError) as exc:
        print(f"Quota status unavailable (purchasing remains blocked): {exc}")
        return 1
