"""Unit tests; do not perform API or SMTP requests."""

from __future__ import annotations

from pathlib import Path
import tempfile
from urllib.error import HTTPError
from io import BytesIO
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
        self.assertIn('style="display:block;padding:14px 20px', html)
        self.assertIn("Se o botão não abrir, utiliza este link:", html)
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

    def test_backoff_exponential_and_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "api-backoff.json"
            err = monitor.APITransientError("HTTP 429")
            self.assertEqual(monitor.apply_api_failure(path, err, now=1000), 60)
            self.assertTrue(monitor.api_cooldown_active(path, now=1001))
            self.assertEqual(monitor.read_cooldown(path), (1060.0, 1))
            self.assertEqual(monitor.apply_api_failure(path, err, now=1061), 120)
            self.assertEqual(monitor.read_cooldown(path), (1181.0, 2))
            self.assertFalse(monitor.api_cooldown_active(path, now=1182))

    def test_retry_after_header_is_respected(self):
        self.assertEqual(monitor.retry_after_seconds("3600"), 3600)
        self.assertEqual(monitor.retry_after_seconds("invalid"), None)
        self.assertEqual(monitor.retry_after_seconds("Wed, 18 Nov 2026 23:00:00 GMT",
                            now=1795041000), 1800)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "api-backoff.json"
            self.assertEqual(monitor.apply_api_failure(
                path, monitor.APITransientError("429", retry_after=900), now=10), 900)

    def test_http_429_is_transient(self):
        from unittest.mock import MagicMock
        err = HTTPError("https://example.com", 429, "Too Many", {"Retry-After": "360"}, BytesIO(b""))
        with patch.object(monitor, "urlopen", side_effect=err):
            with self.assertRaises(monitor.APITransientError) as caught:
                monitor.retrieve_stock()
            self.assertEqual(caught.exception.retry_after, 360)

    def test_http_403_does_not_cause_backoff(self):
        err = HTTPError("https://example.com", 403, "Forbidden", {}, BytesIO(b""))
        with patch.object(monitor, "urlopen", side_effect=err):
            with self.assertRaises(monitor.MonitorError) as caught:
                monitor.retrieve_stock()
            self.assertNotIsInstance(caught.exception, monitor.APITransientError)

    def test_success_clears_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "api-backoff.json"
            monitor.write_cooldown(path, 0, 3)
            self.assertEqual(monitor.read_cooldown(path), (0.0, 3))
            monitor.write_cooldown(path, 0, 0)
            self.assertEqual(monitor.read_cooldown(path), (0.0, 0))

    def test_alert_includes_catalog_price_and_fallback(self):
        stocks = monitor.parse_stock(response("1H-low", "72H"))
        prices = {monitor.PREFERRED_STORAGE: "42.00 EUR/mês sem IVA (estimativa de catálogo)"}
        _, plain = monitor.alert_content(stocks, prices=prices)
        html = monitor.alert_html(stocks, prices=prices)
        self.assertIn("42.00 EUR/mês sem IVA", plain)
        self.assertIn("42.00 EUR/mês sem IVA", html)
        self.assertIn("Preço indisponível", html)
        self.assertIn("Preço de catálogo indisponível", plain)

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
