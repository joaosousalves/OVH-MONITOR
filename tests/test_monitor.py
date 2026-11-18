"""Unit tests; do not perform API or SMTP requests."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ksle_monitor as monitor


PREFERRED = "27skle06-v1.ram-128g-ddr4-ecc-2400.hybridsoftraid-2x4000sa-2x500nvme"
OTHER = "27skle06-v1.ram-128g-ddr4-ecc-2400.softraid-2x1920nvme"


def response(preferred_status="unavailable", other_status="unavailable", dc="bhs"):
    return [
        {"planCode": monitor.PLAN_CODE, "fqn": PREFERRED,
         "storage": monitor.PREFERRED_STORAGE,
         "datacenters": [{"datacenter": dc, "availability": preferred_status}]},
        {"planCode": monitor.PLAN_CODE, "fqn": OTHER,
         "storage": "softraid-2x1920nvme",
         "datacenters": [{"datacenter": "gra", "availability": other_status}]},
    ]


class MonitorTests(unittest.TestCase):
    def test_correct_matching_and_preference_first(self):
        stocks = monitor.parse_stock(response("1H-low", "72H"))
        self.assertEqual(len(stocks), 2)
        self.assertTrue(stocks[0].preferred)
        self.assertEqual(stocks[0].datacenter, "bhs")
        self.assertTrue(all(s.available for s in stocks))

    def test_non_orderable_statuses_are_not_available(self):
        for status in ["unavailable", "unknown", "comingSoon", "weird"]:
            with self.subTest(status=status):
                self.assertFalse(monitor.Stock(PREFERRED, monitor.PREFERRED_STORAGE, "bhs", status).available)

    def test_invalid_payload_cannot_reset_history(self):
        for bad in [{}, [], [{"planCode": "other", "datacenters": []}]]:
            with self.subTest(bad=bad):
                with self.assertRaises(monitor.MonitorError):
                    monitor.parse_stock(bad)

    def test_alert_once_then_after_unavailable(self):
        first = monitor.parse_stock(response("1H-high"))
        new, current = monitor.transition(first, set())
        self.assertEqual(len(new), 1)
        current.update(s.key for s in new)
        again, unchanged = monitor.transition(first, current)
        self.assertFalse(again)
        self.assertEqual(current, unchanged)
        sold_out = monitor.parse_stock(response("unavailable"))
        reset_new, reset_state = monitor.transition(sold_out, unchanged)
        self.assertFalse(reset_new)
        self.assertFalse(reset_state)
        restock_new, _ = monitor.transition(first, reset_state)
        self.assertEqual(len(restock_new), 1)

    def test_missing_datacenter_does_not_reset_already_alerted(self):
        first = monitor.parse_stock(response("1H-high"))
        key = first[0].key
        missing_bhs = monitor.parse_stock(response("unavailable", "unavailable", dc="rbx"))
        new, current = monitor.transition(missing_bhs, {key})
        self.assertFalse(new)
        self.assertEqual(current, {key})

    def test_state_persists_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.assertFalse(monitor.load_alerted(path))
            monitor.save_alerted(path, {"one@bhs"})
            self.assertEqual(monitor.load_alerted(path), {"one@bhs"})

    def test_bad_state_refuses_to_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("invalid json", encoding="utf-8")
            with self.assertRaises(monitor.MonitorError):
                monitor.load_alerted(path)

    def test_email_failure_does_not_mark_as_sent(self):
        stocks = monitor.parse_stock(response("1H-low"))
        dummy = monitor.MailConfig("smtp", 587, "starttls", "", "", "sender@example.com", ("to@example.com",))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with patch.object(monitor, "send_mail", side_effect=monitor.MonitorError("smtp failed")):
                with self.assertRaises(monitor.MonitorError):
                    monitor.run_once(stocks, path, dummy, dry_run=False)
            self.assertFalse(monitor.load_alerted(path))

    def test_success_sends_one_email_and_updates_state(self):
        stocks = monitor.parse_stock(response("1H-high", "72H"))
        dummy = monitor.MailConfig("smtp", 587, "starttls", "", "", "sender@example.com", ("to@example.com",))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with patch.object(monitor, "send_mail") as send:
                monitor.run_once(stocks, path, dummy, dry_run=False)
                monitor.run_once(stocks, path, dummy, dry_run=False)
                send.assert_called_once()
                self.assertEqual(len(monitor.load_alerted(path)), 2)
                self.assertIn("preferred disks", send.call_args.args[1])

    def test_check_does_not_send_or_write_state(self):
        stocks = monitor.parse_stock(response("1H-low"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with patch.object(monitor, "send_mail") as send:
                self.assertEqual(monitor.run_once(stocks, path, None, dry_run=True), 0)
                send.assert_not_called()
            self.assertFalse(path.exists())


    def test_html_content_and_preference_badge(self):
        stocks = monitor.parse_stock(response("1H-high", "72H"))
        html = monitor.alert_html(stocks)
        self.assertIn("DISCOS PREFERIDOS", html)
        self.assertIn("BHS", html)
        self.assertIn("GRA", html)
        self.assertIn("Ver servidor na OVHcloud", html)
        self.assertIn(monitor.PRODUCT_URL, html)
        self.assertLess(html.index("DISCOS PREFERIDOS"), html.index("2 x 1.92 TB NVMe"))

    def test_html_escapes_api_values(self):
        malicious = monitor.Stock("fqn", monitor.PREFERRED_STORAGE, '<script>alert(1)</script>', '1H-high')
        html = monitor.alert_html([malicious], test=True)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;SCRIPT&gt;", html)
        self.assertIn("EMAIL DE TESTE", html)

    def test_multipart_email_contains_plain_and_html(self):
        config = monitor.MailConfig("smtp", 587, "starttls", "", "", "sender@example.com", ("to@example.com",))
        with patch.object(monitor.smtplib, "SMTP") as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            monitor.send_mail(config, "Subject", "Plain fallback", "<b>HTML</b>")
            message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertEqual(message.get_content_type(), "multipart/alternative")
            self.assertEqual(message.get_body(preferencelist=("plain",)).get_content().strip(), "Plain fallback")
            self.assertIn("<b>HTML</b>", message.get_body(preferencelist=("html",)).get_content())

    def test_partially_refused_smtp_recipients_is_failure(self):
        config = monitor.MailConfig("smtp", 587, "starttls", "", "", "sender@example.com", ("a@example.com",))
        with patch.object(monitor.smtplib, "SMTP") as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {
                "a@example.com": (550, b"Invalid recipient")
            }
            with self.assertRaises(monitor.MonitorError):
                monitor.send_mail(config, "test", "hello")


if __name__ == "__main__":
    unittest.main()
