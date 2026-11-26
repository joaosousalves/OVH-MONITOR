"""One retained commercial cart, never an order or proof of stock reservation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tempfile
from datetime import datetime, timezone
from urllib.error import HTTPError

from order_probe import OrderProbeError
from ksle_monitor import ALLOWED_DATACENTERS, PLAN_CODE, REQUIRED_MEMORY, STORAGE_PRIORITIES


class RetainedCartError(OrderProbeError):
    pass


def _expiry(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _validate(record):
    if not isinstance(record, dict) or record.get("schema") != 1:
        raise RetainedCartError("Invalid retained cart state")
    if (not isinstance(record.get("cart_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", record["cart_id"])
            or type(record.get("item_id")) is not int or record["item_id"] <= 0
            or record.get("fqn") not in {
                f"{PLAN_CODE}.{REQUIRED_MEMORY}.{storage}" for storage in STORAGE_PRIORITIES}
            or record.get("datacenter") not in ALLOWED_DATACENTERS):
        raise RetainedCartError("Invalid retained cart identity")
    return record


def read_retained(path: Path):
    path = Path(path)
    if path.is_symlink():
        raise RetainedCartError("Retained cart state cannot be a symlink")
    try:
        meta = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(meta.st_mode) or meta.st_mode & 0o077 or meta.st_size > 4096:
        raise RetainedCartError("Unsafe retained cart state")
    try:
        return _validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError) as exc:
        raise RetainedCartError("Unreadable retained cart state") from exc


def persist_retained(path: Path, *, cart_id, item_id, fqn, datacenter, expire):
    """Atomic no-overwrite handoff; caller holds commercial watcher lock."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise RetainedCartError("Retained cart directory must be private")
    record = _validate({"schema": 1, "cart_id": cart_id, "item_id": item_id,
                        "fqn": fqn, "datacenter": datacenter,
                        "expire": expire if _expiry(expire) is not None else None,
                        "created_at": datetime.now(timezone.utc).isoformat()})
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(record, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise RetainedCartError("Cannot persist retained cart; no new order") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def ensure_no_active_cart(path: Path, client, *, now=None):
    """Allow another cart only after documented expiry or explicit 404/410.

    Never deletes a remote cart; a 403, timeout or ambiguous reply blocks
    creation. Cart state remains separate from the actual order ledger.
    """
    path = Path(path)
    record = read_retained(path)
    if record is None:
        return
    now = datetime.now(timezone.utc) if now is None else now
    try:
        response = client.request("GET", "/order/cart/" + record["cart_id"])
    except OrderProbeError as exc:
        cause = exc.__cause__
        if not isinstance(cause, HTTPError) or cause.code not in (404, 410):
            raise RetainedCartError("Retained cart status unknown; no duplicate cart") from exc
    else:
        if not isinstance(response, dict) or response.get("cartId") != record["cart_id"]:
            raise RetainedCartError("Retained cart readback incomplete; no duplicate cart")
        expiry = _expiry(response.get("expire"))
        if expiry is None or expiry > now:
            raise RetainedCartError("Existing retained cart requires review; no duplicate cart")
    path.unlink()
