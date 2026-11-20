"""Offline watchdog tests: restart-safe transitions, no SMTP duplicates."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import commercial_watch as watcher
import health_monitor as health
import ksle_monitor as monitor
from purchase_guard import APPROVED_FQNS, STORAGE_PRIORITIES


class HealthMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state = root / "health" / "status.json"
        self.stock = root / "stock" / "heartbeat.json"
        self.commercial = root / "commercial" / "heartbeat.json"
        self.notify = Mock()

    def check(self, now=1000):
        return health.check_health(
            state_path=self.state, stock_path=self.stock,
            commercial_path=self.commercial, notify=self.notify, now=now,
        )

    def test_bootstrap_with_live_heartbeats_does_not_email(self):
        health.record_heartbeat(self.stock, now=950)
        health.record_heartbeat(self.commercial, now=950)
        self.assertIn("HEALTHY", self.check(1000))
        self.assertIn("UNCHANGED", self.check(1100))
        self.notify.assert_not_called()
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.stock.stat().st_mode & 0o777, 0o600)

    def test_missing_heartbeats_startup_grace_then_one_warning(self):
        self.assertIn("HEALTHY", self.check(1000))
        self.assertIn("UNCHANGED", self.check(3000))
        self.notify.assert_not_called()
        self.assertIn("SENT", self.check(3500))
        self.assertIn("FALHA", self.notify.call_args.args[0])
        self.assertIn("no successful poll", self.notify.call_args.args[1])
        self.assertIn("UNCHANGED", self.check(5000))
        self.notify.assert_called_once()

    def test_stale_to_recovery_to_failure_single_alert_per_edge(self):
        health.record_heartbeat(self.stock, now=990)
        health.record_heartbeat(self.commercial, now=990)
        self.check(1000)
        self.assertIn("SENT", self.check(4000))
        self.assertIn("UNCHANGED", self.check(4010))
        self.assertEqual(self.notify.call_count, 1)
        health.record_heartbeat(self.stock, now=4100)
        health.record_heartbeat(self.commercial, now=4100)
        self.assertIn("SENT", self.check(4200))
        self.assertIn("RECUPERADO", self.notify.call_args.args[0])
        self.assertIn("UNCHANGED", self.check(4300))
        self.assertEqual(self.notify.call_count, 2)

    def test_single_commercial_failure_is_reported(self):
        health.record_heartbeat(self.stock, now=4000)
        health.record_heartbeat(self.commercial, now=1000)
        self.check(4000)
        self.assertEqual(self.notify.call_count, 1)
        self.assertIn("commercial", self.notify.call_args.args[1])
        self.assertNotIn("stock:", self.notify.call_args.args[1])

    def test_smtp_uncertainty_is_not_automatically_retried(self):
        health.record_heartbeat(self.stock, now=1000)
        health.record_heartbeat(self.commercial, now=1000)
        self.check(1000)
        failure = Mock(side_effect=monitor.MonitorError("SMTP timed out"))
        with self.assertRaisesRegex(monitor.MonitorError, "SMTP timed out"):
            health.check_health(
                state_path=self.state, stock_path=self.stock,
                commercial_path=self.commercial, notify=failure, now=5000,
            )
        self.assertIn("REVIEW REQUIRED", self.check(6000))
        self.notify.assert_not_called()
        failure.assert_called_once()
        with patch.dict("os.environ", {"OVH_HEALTH_STATE": str(self.state)}):
            with patch("builtins.print") as printed:
                self.assertEqual(health.run_health_status(), 0)
        self.assertTrue(any("REVIEW REQUIRED" in str(x) for x in printed.call_args_list))

    def test_insecure_or_corrupted_heartbeat_reports_failure(self):
        self.stock.parent.mkdir()
        self.stock.write_text('{"schema":1,"checked_at":5}', encoding="utf-8")
        self.stock.chmod(0o644)
        health.record_heartbeat(self.commercial, now=1000)
        self.check(1000)
        self.assertIn("FALHA", self.notify.call_args.args[0])
        with self.assertRaisesRegex(health.HealthError, "private 0600"):
            health.record_heartbeat(self.stock, now=1100)

    def test_heartbeat_malformed_schema_and_symlink_rejected(self):
        self.stock.parent.mkdir()
        self.stock.write_text('{"schema":7,"checked_at":500}', encoding="utf-8")
        self.stock.chmod(0o600)
        with self.assertRaisesRegex(health.HealthError, "Malformed"):
            health.read_heartbeat(self.stock)
        linked = self.stock.parent / "other.json"
        linked.symlink_to(self.stock)
        with self.assertRaisesRegex(health.HealthError, "Unsafe symlink"):
            health.read_heartbeat(linked)

    def test_monitor_records_only_completed_real_polls(self):
        path = self.stock.parent / "alert-state.json"
        config = monitor.MailConfig("", 0, "", "", "", "", ())
        stock = monitor.Stock(APPROVED_FQNS[0], STORAGE_PRIORITIES[0], "bhs", "1H-high")
        heartbeat = path.with_name("health-heartbeat.json")
        with patch.object(monitor, "send_mail"):
            monitor.run_once([stock], path, config, False)
        self.assertIsNotNone(health.read_heartbeat(heartbeat))
        old = health.read_heartbeat(heartbeat)
        with patch.object(monitor, "send_mail"):
            monitor.run_once([stock], path, None, True)
        self.assertEqual(health.read_heartbeat(heartbeat), old)

    def test_failed_smtp_produces_no_heartbeat(self):
        path = self.stock.parent / "alert-state.json"
        config = monitor.MailConfig("", 0, "", "", "", "", ())
        stock = monitor.Stock(APPROVED_FQNS[0], STORAGE_PRIORITIES[0], "bhs", "1H-high")
        with patch.object(monitor, "send_mail", side_effect=monitor.MonitorError("smtp")):
            with self.assertRaises(monitor.MonitorError):
                monitor.run_once([stock], path, config, False)
        self.assertIsNone(health.read_heartbeat(path.with_name("health-heartbeat.json")))

    def test_health_cli_does_not_touch_api(self):
        with patch.object(monitor, "retrieve_stock", side_effect=AssertionError("API called")):
            with patch.object(health, "run_health_status", return_value=0) as status:
                self.assertEqual(monitor.main(["--health-status"]), 0)
        status.assert_called_once()


if __name__ == "__main__":
    unittest.main()
