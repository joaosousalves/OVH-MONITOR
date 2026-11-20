"""Persistent commercial watcher: offline, no API/SMTP/checkout traffic."""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import commercial_watch as watcher
import ksle_monitor as monitor
from purchase_guard import APPROVED_FQNS, STORAGE_PRIORITIES, PurchasePolicy, PurchaseSafetyError


def policy():
    return PurchasePolicy.from_env({
        "OVH_AUTO_BUY_ENABLED": "false",
        "OVH_AUTO_BUY_MAX_TOTAL_EUR": "80.00",
        "OVH_AUTO_BUY_MAX_MONTHLY_EUR": "37.00",
    })


def stock(*, index=0, dc="bhs", status="1H-high"):
    return monitor.Stock(APPROVED_FQNS[index], STORAGE_PRIORITIES[index], dc, status)


REPORT = (
    "COMMERCIAL AUDIT PARTIAL (NO PURCHASE): initial 49.20 EUR incl VAT; "
    "monthly 37 EUR ceiling NOT VERIFIED; legal terms NOT VERIFIED. "
    "Checkout/payment remain DISABLED."
)


class CommercialWatcherTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "watch.json"
        self.audit = Mock(return_value=REPORT)
        self.notify = Mock()

    def run_watch(self, stocks=None, now=1000.0, audit=None, notify=None, selected_policy=None):
        return watcher.watch_once(
            selected_policy or policy(),
            stocks=stocks if stocks is not None else [stock()],
            path=self.path,
            audit=audit if audit is not None else self.audit,
            notify=notify if notify is not None else self.notify,
            now=now,
        )

    def test_no_stock_never_calls_cart_or_smtp(self):
        self.assertIn("WAITING", self.run_watch([stock(status="unavailable")]))
        self.audit.assert_not_called()
        self.notify.assert_not_called()

    def test_first_availability_runs_once_emails_once_and_persists(self):
        self.assertIn("SENT", self.run_watch())
        self.assertEqual(self.audit.call_count, 1)
        self.notify.assert_called_once()
        self.assertEqual(self.notify.call_args.args[1], REPORT)
        self.assertIn("UNCHANGED", self.run_watch(now=2000))
        self.assertEqual(self.audit.call_count, 1)
        self.assertEqual(self.notify.call_count, 1)
        state = watcher.read_state(self.path)
        self.assertEqual(state["attempt"]["status"], "complete")
        self.assertTrue(state["attempt"]["emailed"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_stock_becomes_unavailable_then_reappears_after_cooldown(self):
        self.run_watch(now=1000)
        self.assertIn("WAITING", self.run_watch([stock(status="unavailable")], now=1010))
        self.assertIn("COOLDOWN", self.run_watch(now=1050))
        self.assertIn("SENT", self.run_watch(now=2000))
        self.assertEqual(self.audit.call_count, 2)
        self.assertEqual(self.notify.call_count, 2)

    def test_failed_audit_retries_with_backoff_and_no_email(self):
        def failed():
            raise PurchaseSafetyError("Temporary quote problem")
        self.assertIn("RETRY LATER", self.run_watch(audit=failed, now=1000))
        self.assertIn("COOLDOWN", self.run_watch(now=1899))
        self.assertEqual(self.audit.call_count, 0)
        self.assertIn("SENT", self.run_watch(now=1901))
        self.assertEqual(self.audit.call_count, 1)
        self.assertEqual(self.notify.call_count, 1)

    def test_crash_during_cart_never_immediately_retries(self):
        state = watcher._blank_state()
        state["seen"] = [stock().key]
        state["last_attempt_at"] = 1000.0
        state["attempt"] = {"key": stock().key, "status": "started", "failures": 0}
        watcher.save_state(self.path, state)
        self.assertIn("COOLDOWN", self.run_watch(now=1010))
        self.audit.assert_not_called()
        self.assertIn("SENT", self.run_watch(now=1901))

    def test_uncertain_smtp_does_not_resend_or_rebuild_cart(self):
        def uncertain(_choice, _report):
            raise monitor.MonitorError("SMTP outcome uncertain")
        with self.assertRaisesRegex(monitor.MonitorError, "SMTP outcome uncertain"):
            self.run_watch(notify=uncertain)
        state = watcher.read_state(self.path)
        self.assertEqual(state["attempt"]["status"], "sending")
        self.assertFalse(state["attempt"]["emailed"])
        self.assertIn("REVIEW REQUIRED", self.run_watch(now=3000))
        self.assertIn("REVIEW REQUIRED", self.run_watch(
            stocks=[stock(status="unavailable")], now=4000,
        ))
        self.assertEqual(watcher.read_state(self.path)["attempt"]["status"], "sending")
        self.assertEqual(self.audit.call_count, 1)
        self.notify.assert_not_called()

    def test_saved_undelivered_report_is_sent_without_requoting(self):
        state = watcher._blank_state()
        state["seen"] = [stock().key]
        state["attempt"] = {
            "key": stock().key, "status": "complete", "failures": 0,
            "report": REPORT, "emailed": False,
        }
        state["last_attempt_at"] = 1000.0
        watcher.save_state(self.path, state)
        self.assertIn("SENT", self.run_watch(now=1010))
        self.audit.assert_not_called()
        self.notify.assert_called_once()

    def test_corrupted_private_state_fails_closed_without_cart(self):
        self.path.write_text('{"schema": 999}', encoding="utf-8")
        self.path.chmod(0o600)
        with self.assertRaisesRegex(watcher.WatchStateError, "schema"):
            self.run_watch()
        self.audit.assert_not_called()

    def test_public_state_permissions_fail_closed(self):
        self.path.write_text(json.dumps(watcher._blank_state()), encoding="utf-8")
        self.path.chmod(0o644)
        with self.assertRaisesRegex(watcher.WatchStateError, "private 0600"):
            self.run_watch()
        self.audit.assert_not_called()

    def test_choose_preferred_disk_and_allowed_datacenter_only(self):
        first = stock(index=0, dc="bhs", status="unavailable")
        alt = stock(index=1, dc="fra")
        bad = stock(index=0, dc="sgp")
        self.assertIn("SENT", self.run_watch([first, alt, bad]))
        self.assertEqual(self.notify.call_args.args[0].key, alt.key)
        state = watcher.read_state(self.path)
        self.assertEqual(state["seen"], [alt.key])

    def test_enabled_purchase_policy_is_never_accepted(self):
        unsafe = PurchasePolicy(
            True, Decimal("80.00"), ("bhs",), max_monthly_eur=Decimal("37.00"),
        )
        with self.assertRaisesRegex(PurchaseSafetyError, "forbidden"):
            self.run_watch(selected_policy=unsafe)
        self.audit.assert_not_called()

    def test_incomplete_quote_never_emailed(self):
        audit = Mock(return_value="LIMIT REACHED: remote cap 2/2")
        self.assertIn("RETRY LATER", self.run_watch(audit=audit))
        self.notify.assert_not_called()
        self.assertEqual(watcher.read_state(self.path)["attempt"]["status"], "failed")

    def test_cli_dispatch_and_status_do_not_send_automatic_emails(self):
        with patch.object(watcher, "run_commercial_watch", return_value=0) as run:
            with patch.object(monitor, "send_mail") as send:
                self.assertEqual(monitor.main(["--commercial-watch"]), 0)
        run.assert_called_once()
        send.assert_not_called()
        with patch.object(watcher, "run_commercial_watch_status", return_value=0) as status:
            self.assertEqual(monitor.main(["--commercial-watch-status"]), 0)
        status.assert_called_once()

    def test_read_only_status_command_reports_ambiguous_smtp(self):
        state = watcher._blank_state()
        state["seen"] = [stock().key]
        state["attempt"] = {
            "key": stock().key, "status": "sending", "failures": 0,
            "report": REPORT, "emailed": False,
        }
        watcher.save_state(self.path, state)
        with patch.dict("os.environ", {"OVH_COMMERCIAL_WATCH_STATE": str(self.path)}):
            with patch("builtins.print") as printed:
                self.assertEqual(watcher.run_commercial_watch_status(), 0)
        self.assertTrue(any("REVIEW REQUIRED" in str(c) for c in printed.call_args_list))


if __name__ == "__main__":
    unittest.main()
