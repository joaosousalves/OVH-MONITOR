"""Public read-only six-model OVHcloud inventory reporting tests."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import eco_discovery as discovery
import ksle_monitor as monitor


def sample(plan, *, avail="1H-high", dc="bhs"):
    return [
        monitor.Stock(
            fqn=f"{plan}.ram-64g-ecc-2400.softraid-2x512nvme",
            storage="softraid-2x512nvme", datacenter=dc, status=avail
        )
    ]


class EcoDiscoveryTests(unittest.TestCase):
    def test_checks_six_expected_plans_without_private_credentials(self):
        checked = []
        def fake_stock(*, plan_code):
            checked.append(plan_code)
            return sample(plan_code)
        report, good = discovery.render_options(fetch=fake_stock,
                                               sleep=lambda _: None, pause_seconds=0)
        self.assertTrue(good)
        self.assertEqual(checked, list(discovery.PLANS.values()))
        self.assertIn("KS-LE-1", report)
        self.assertIn("KS-LE-6", report)
        self.assertIn("RAM=ram-64g", report)
        self.assertIn("FQN=27skle06-v1.", report)
        self.assertIn("not number of units", report)

    def test_unavailable_is_not_counted_as_available(self):
        def fake_stock(*, plan_code):
            return sample(plan_code, avail="unavailable")
        report, good = discovery.render_options(fetch=fake_stock,
                                               sleep=lambda _: None, pause_seconds=0)
        self.assertTrue(good)
        self.assertEqual(report.count("No currently orderable configuration"), 6)
        self.assertNotIn("RAM=", report)

    def test_rate_limit_stops_requests_without_claiming_out_of_stock(self):
        visited = []
        def fake_stock(*, plan_code):
            visited.append(plan_code)
            if plan_code == "27skle02-v1":
                raise monitor.APITransientError("HTTP 429")
            return sample(plan_code)
        report, good = discovery.render_options(fetch=fake_stock,
                                               sleep=lambda _: None, pause_seconds=0)
        self.assertFalse(good)
        self.assertEqual(visited, ["27skle01-v1", "27skle02-v1"])
        self.assertIn("KS-LE-2 | 27skle02-v1 | CHECK FAILED: HTTP 429", report)
        self.assertIn("Remaining plans NOT CHECKED", report)
        self.assertNotIn("KS-LE-6 |", report)

    def test_mismatched_plan_or_empty_response_rejected(self):
        for response in ([], sample("other-plan")):
            with self.subTest(response=response):
                report, good = discovery.render_options(
                    fetch=lambda **kwargs: response, sleep=lambda _: None,
                    pause_seconds=0
                )
                self.assertFalse(good)
                self.assertIn("CHECK FAILED", report)

    def test_cli_does_not_send_mail_or_create_order(self):
        def fake_stock(*, plan_code):
            return sample(plan_code)
        with patch.object(discovery, "retrieve_stock", side_effect=AssertionError("unexpected")):
            with patch.object(discovery, "render_options", return_value=("read-only report", True)):
                with patch("builtins.print") as output:
                    self.assertEqual(monitor.main(["--list-eco-options"]), 0)
        output.assert_called_once_with("read-only report")

if __name__ == "__main__":
    unittest.main()
