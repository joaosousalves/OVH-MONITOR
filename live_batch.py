"""Two independent KS-LE-6 carts, one durable global quota, parallel checkout.

A successful response means a known order ID, not confirmed payment.
No uncertain checkout is ever retried automatically.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import logging
import os
from pathlib import Path
import stat
import time

from commitment_guard import CommitmentSafetyError
from order_probe import OrderProbeError
from pending_orders import reconcile_pending_orders
from purchase_guard import PurchaseSafetyError, choose_candidate
from retained_cart import read_retained, ensure_no_active_cart, RetainedCartError
from live_purchase import enabled, prepare_live_cart, verify_retained

LOG = logging.getLogger("ksle-live-batch")


def effective_usage(entries, remote):
    """Conservative union: count known local orders not proved in inventory."""
    pending = set(remote.target_pending_ids)
    delivered = set(remote.delivered_ids)
    delivered_local = [r for r in entries if r.get("orderId") in delivered]
    covered_delivered = (len(delivered_local)
                         if remote.services.matching_services >= len(delivered_local) else 0)
    covered_pending = sum(r.get("orderId") in pending for r in entries)
    return remote.upper_bound + len(entries) - covered_delivered - covered_pending


def _paths(directory):
    return [directory / "retained-cart.json", directory / "retained-cart-2.json"]


def _active_record(path, client, entries):
    record = read_retained(path)
    if record is None:
        return None
    if any(row.get("cartId") == record["cart_id"] for row in entries):
        # Acknowledged order is owned by OVH; remove only local handoff.
        path.unlink()
        return None
    try:
        ensure_no_active_cart(path, client)
    except RetainedCartError as exc:
        if str(exc) != "Existing retained cart requires review; no duplicate cart":
            raise
        return record
    return None


def _mark_delivery(quota, entries, remote):
    delivered = set(remote.delivered_ids)
    known = [r for r in entries if r.get("orderId") in delivered]
    if remote.services.matching_services >= len(known) and not remote.overlap_unverified:
        for row in known:
            if row["status"] == "submitted":
                quota.update_outcome(slot=row["slot"], status="confirmed", order_id=row["orderId"])
    return quota.read()


def _submit_one(client, quota, slot, record, path):
    started = time.monotonic()
    try:
        response = client._submit_live_checkout(record["cart_id"])
        order_id = response.get("orderId") if isinstance(response, dict) else None
        if type(order_id) is not int or not 0 < order_id < 10**15:
            raise OrderProbeError("No valid order identifier")
        quota.update_outcome(slot=slot, status="submitted", order_id=order_id)
    except Exception:
        # A failed journal write or network request may have followed a real
        # order. Preserve reserved/unknown rather than repeating the POST.
        current = next(r for r in quota.read() if r["slot"] == slot)
        if current["status"] == "reserved":
            quota.update_outcome(slot=slot, status="unknown")
        return f"LIVE CHECKOUT UNKNOWN: slot {slot}; inspect OVH Manager; no automatic retry"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOG.warning("Submitted cart handoff cleanup deferred; durable order retained")
    return (f"LIVE CHECKOUT SUBMITTED: slot {slot}; order {order_id}; "
            f"{record['fqn']} at {record['datacenter'].upper()}; "
            f"payment requested, NOT confirmed; POST {time.monotonic()-started:.2f}s")


def run_live_batch(policy, stocks, journal, quota, directory, client_factory):
    """Called by the live watcher under a shared lock; no SMTP before checkout."""
    if not enabled():
        raise PurchaseSafetyError("Live checkout disabled")
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise PurchaseSafetyError("Live checkout directory not private")
    fd = os.open(directory / "live-checkout.lock",
                 os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_mode & 0o077:
            raise PurchaseSafetyError("Unsafe live checkout lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        if journal.read() is not None:
            raise PurchaseSafetyError("Unresolved legacy purchase journal")
        quota._check_legacy()
        entries = quota.read()
        if any(r["status"] in ("reserved", "unknown") for r in entries):
            return "LIVE CHECKOUT BLOCKED: uncertain earlier checkout; manual reconciliation required"
        paths = _paths(directory)
        candidate = choose_candidate(stocks, policy)
        no_work = candidate is None and not any(p.exists() or p.is_symlink() for p in paths)
        if len(entries) >= policy.max_servers or no_work:
            # Do not create carts when the quota is full or there is no stock.
            # Poll the two known order IDs only; full inventory on delivery.
            submitted = [r for r in entries if r["status"] == "submitted"]
            if submitted:
                client = client_factory()
                states = [client.request("GET", f"/me/order/{r['orderId']}/status") for r in submitted]
                if any((v.get("status") if isinstance(v, dict) else v) == "delivered" for v in states):
                    remote = reconcile_pending_orders(client, policy.max_servers, read_workers=3)
                    entries = _mark_delivery(quota, entries, remote)
            if len(entries) >= policy.max_servers:
                return "LIVE CHECKOUT: quota reached; " + str(quota.counts(entries))
            return "WAITING: no eligible KS-LE-6 stock; " + str(quota.counts(entries))
        client = client_factory()
        active = []
        for path in paths:
            record = _active_record(path, client, entries)
            if record is not None:
                active.append((path, record, client_factory()))
        local_remaining = policy.max_servers - len(entries)
        # Never build more carts than the number of remaining local slots.
        vacant = [p for p in paths if all(p != a[0] for a in active)]
        plans = []
        excluded = {f"{r['fqn']}@{r['datacenter']}" for _, r, _ in active}
        for path in vacant[:max(0, local_remaining-len(active))]:
            choice = choose_candidate([s for s in stocks if s.key not in excluded], policy) or candidate
            if choice is None:
                break
            excluded.add(choice.key)
            plans.append((path, choice, client_factory()))

        def build(plan):
            path, choice, cart_client = plan
            try:
                prepare_live_cart(policy, cart_client, [choice], journal, quota, path,
                                  allow_submitted=True)
                record = read_retained(path)
                if record is not None:
                    return (path, record, cart_client)
            except (PurchaseSafetyError, OrderProbeError, CommitmentSafetyError):
                LOG.warning("Live batch cart preparation blocked for %s at %s", choice.fqn, choice.datacenter)
            return None

        if plans:
            with ThreadPoolExecutor(max_workers=len(plans)) as pool:
                active.extend(r for r in pool.map(build, plans) if r is not None)
        if not active:
            return "LIVE CHECKOUT BLOCKED: no validated retained cart"
        # One fresh complete inventory before reserving a bounded batch.
        remote = reconcile_pending_orders(client, policy.max_servers, read_workers=3)
        entries = _mark_delivery(quota, entries, remote)
        remaining = min(policy.max_servers-len(entries),
                        policy.max_servers-effective_usage(entries, remote))
        if remote.overlap_unverified or remaining <= 0:
            return "LIVE CHECKOUT BLOCKED: remote quota full or inconclusive"
        def verify(item):
            path, record, cart_client = item
            try:
                total = verify_retained(cart_client, policy, record,
                                        path.with_name(path.stem + "-contract-review.json"))
                return (path, record, cart_client, total)
            except (PurchaseSafetyError, OrderProbeError, CommitmentSafetyError):
                LOG.warning("Live batch final verification blocked for %s at %s",
                            record["fqn"], record["datacenter"])
                return None

        # Independent cart reads can run concurrently. The final quotes are
        # refreshed close together, then the global quota is reserved once.
        with ThreadPoolExecutor(max_workers=min(2, len(active))) as pool:
            validated = [v for v in pool.map(verify, active) if v is not None][:remaining]
        if not validated:
            return "LIVE CHECKOUT BLOCKED: no cart passed final verification"
        if not enabled():
            raise PurchaseSafetyError("Live checkout disabled")
        slots = quota.reserve_batch([
            {"fqn": r["fqn"], "datacenter": r["datacenter"], "total": total,
             "cart_id": r["cart_id"]} for _, r, _, total in validated
        ], expected_entries=entries)
        # BOTH slots are durably reserved BEFORE either POST. Distinct cart
        # IDs and clients; no payment/delivery wait between submissions.
        with ThreadPoolExecutor(max_workers=len(slots)) as pool:
            jobs = [pool.submit(_submit_one, cart_client, quota, slot, record, path)
                    for slot, (path, record, cart_client, _) in zip(slots, validated)]
            results = [job.result() for job in jobs]
        return "\n".join(results) + "\n" + str(quota.counts(quota.read()))
    finally:
        os.close(fd)
