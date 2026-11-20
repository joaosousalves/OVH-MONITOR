"""Independent watchdog for the two KS-LE-6 systemd timers.

Only successful, non-dry-run stock checks create heartbeats. The watchdog
itself never queries OVHcloud, touches a cart, or attempts payment.
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
from typing import Callable


LOG = logging.getLogger("ksle-health")
DEFAULT_HEALTH_PATH = "/var/lib/ovh-ksle-health/health-state.json"
DEFAULT_STOCK_HEARTBEAT = "/var/lib/ovh-ksle-monitor/health-heartbeat.json"
DEFAULT_COMMERCIAL_HEARTBEAT = "/var/lib/ovh-ksle-purchase/commercial-heartbeat.json"
# Ordinary OVH 429 backoffs can last 15 minutes; only alert when substantially
# longer than a normal cooldown. Commercial checks are scheduled every 5 min.
STOCK_MAX_AGE_SECONDS = 1800
COMMERCIAL_MAX_AGE_SECONDS = 2400
STARTUP_GRACE_SECONDS = 2400


class HealthError(Exception):
    """Corrupt/unsafe health state or heartbeat; fail closed."""


def _read_private_json(path: Path) -> dict | None:
    if path.is_symlink():
        raise HealthError(f"Unsafe symlink: {path}")
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HealthError(f"Cannot inspect {path}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise HealthError(f"Expected private 0600 file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HealthError(f"Invalid JSON in {path}") from exc
    if not isinstance(value, dict):
        raise HealthError(f"Invalid object in {path}")
    return value


def _atomic_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise HealthError("Health directory is not a directory")
    if path.is_symlink():
        raise HealthError("Refusing to overwrite a symlink")
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".health-", delete=False,
        ) as handle:
            temp = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        temp = None
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise HealthError(f"Cannot persist {path}") from exc
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def record_heartbeat(path: Path, *, now: float | None = None) -> None:
    """Persist evidence of a completed real poll; zero remote operations."""
    timestamp = time.time() if now is None else now
    if type(timestamp) not in (int, float) or not 0 < timestamp < 10**13:
        raise HealthError("Invalid heartbeat time")
    path = Path(path)
    # A pre-existing private heartbeat with invalid metadata is suspicious;
    # never silently replace it.
    read_heartbeat(path)
    _atomic_private_json(path, {"schema": 1, "checked_at": timestamp})


def read_heartbeat(path: Path) -> float | None:
    data = _read_private_json(Path(path))
    if data is None:
        return None
    timestamp = data.get("checked_at")
    if data.get("schema") != 1 or type(timestamp) not in (int, float) or not 0 < timestamp < 10**13:
        raise HealthError(f"Malformed heartbeat: {path}")
    return float(timestamp)


def _freshness(label: str, path: Path, threshold: int, now: float,
               first_seen_at: float) -> str | None:
    try:
        timestamp = read_heartbeat(path)
    except HealthError as exc:
        return f"{label}: invalid heartbeat ({exc})"
    if timestamp is None:
        if now - first_seen_at <= STARTUP_GRACE_SECONDS:
            return None
        return f"{label}: no successful poll heartbeat after startup grace"
    age = now - timestamp
    if age < -300:
        return f"{label}: heartbeat timestamp is in the future"
    if age > threshold:
        return f"{label}: last successful poll {int(age)}s ago (limit {threshold}s)"
    return None


def _initial_state(now: float) -> dict:
    return {"schema": 1, "first_seen_at": now, "condition": None,
            "notification": None}


def _load_state(path: Path, now: float) -> dict:
    state = _read_private_json(path)
    if state is None:
        return _initial_state(now)
    if state.get("schema") != 1 or state.get("condition") not in (None, "healthy", "degraded"):
        raise HealthError("Invalid health state schema/condition")
    first = state.get("first_seen_at")
    if type(first) not in (int, float) or not 0 < first < 10**13:
        raise HealthError("Invalid health state startup timestamp")
    notice = state.get("notification")
    if notice is not None:
        if not isinstance(notice, dict) or notice.get("event") not in ("failure", "recovery") or notice.get("status") not in ("sending", "sent"):
            raise HealthError("Invalid health notification state")
    return state


def check_health(*, state_path: Path, stock_path: Path, commercial_path: Path,
                 notify: Callable[[str, str], None], now: float | None = None) -> str:
    """Send at most one message per state transition, persisting SMTP uncertainty.

    On a crash during SMTP, 'sending' remains in the journal and later runs
    refuse to send again until an operator investigates; no duplicate email.
    """
    now = time.time() if now is None else now
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    lock_path = state_path.with_name(state_path.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise HealthError("Unsafe health lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        state = _load_state(state_path, now)
        if state["notification"] is not None and state["notification"]["status"] == "sending":
            return "REVIEW REQUIRED: health SMTP outcome uncertain; alerts suppressed."
        issues = [issue for issue in (
            _freshness("stock", Path(stock_path), STOCK_MAX_AGE_SECONDS, now, state["first_seen_at"]),
            _freshness("commercial", Path(commercial_path), COMMERCIAL_MAX_AGE_SECONDS, now, state["first_seen_at"]),
        ) if issue]
        condition = "degraded" if issues else "healthy"
        previous = state["condition"]
        if previous == condition:
            return f"UNCHANGED: {condition}; no health email."
        state["condition"] = condition
        if previous is None and condition == "healthy":
            _atomic_private_json(state_path, state)
            return "HEALTHY: baseline established; no health email."
        # A baseline that begins degraded warrants one warning, but normal
        # first-run startup is given a generous grace period above.
        event = "failure" if condition == "degraded" else "recovery"
        reason = "; ".join(issues) if issues else "Both monitors have recent successful checks."
        subject = f"[OVH MONITOR] {'FALHA' if issues else 'RECUPERADO'}: KS-LE-6"
        body = (f"Estado: {condition.upper()}\n\n{reason}\n\n"
                "Sem encomendas, pagamentos ou reservas. Verifique os timers e logs no VPS.\n")
        state["notification"] = {"event": event, "status": "sending"}
        _atomic_private_json(state_path, state)
        notify(subject, body)
        state["notification"]["status"] = "sent"
        _atomic_private_json(state_path, state)
        return f"SENT: health {event} email; current status={condition}."
    finally:
        os.close(fd)


def run_health_check() -> int:
    """One-shot independent systemd watchdog; SMTP only on change."""
    from ksle_monitor import MonitorError, mail_config, send_mail
    try:
        result = check_health(
            state_path=Path(os.getenv("OVH_HEALTH_STATE", DEFAULT_HEALTH_PATH)),
            stock_path=Path(os.getenv("OVH_STOCK_HEARTBEAT", DEFAULT_STOCK_HEARTBEAT)),
            commercial_path=Path(os.getenv("OVH_COMMERCIAL_HEARTBEAT", DEFAULT_COMMERCIAL_HEARTBEAT)),
            notify=lambda subject, body: send_mail(mail_config(), subject, body),
        )
        LOG.info(result)
        return 0
    except (HealthError, MonitorError, OSError, ValueError) as exc:
        LOG.error("Health watchdog failed safely: %s", exc)
        return 1


def run_health_status() -> int:
    """Show the watchdog's last decision, without querying services or email."""
    try:
        path = Path(os.getenv("OVH_HEALTH_STATE", DEFAULT_HEALTH_PATH))
        state = _read_private_json(path)
        if state is None:
            print("HEALTH: not initialized. No watchdog run recorded.")
        else:
            state = _load_state(path, time.time())
            notice = state["notification"]
            print(f"HEALTH: last state={state['condition'] or 'unknown'}; "
                  f"last notification={notice['event'] + '/' + notice['status'] if notice else 'none'}.")
            if notice is not None and notice["status"] == "sending":
                print("REVIEW REQUIRED: SMTP outcome uncertain. Check mailbox/logs before any manual reset.")
        return 0
    except (HealthError, OSError) as exc:
        LOG.error("Health journal unreadable: %s", exc)
        return 1
