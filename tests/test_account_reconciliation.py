"""Read-only account inventory tests: all OVH responses are fakes."""
import sys
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from account_reconciliation import (
    AccountReconciliationError, analyze_services, reconcile_account,
)
import ksle_monitor as monitor
from order_probe import OVHCartClient, OrderProbeError


def service(code="27skle06-v1", quantity=1):
    return {"plan": {"code": code}, "quantity": quantity, "state": "ok"}


class FakeAccount:
    def __init__(self, ids, details):
        self.ids, self.details, self.calls = ids, details, []

    def request(self, method, path):
        self.calls.append((method, path))
        if method != "GET":
            raise AssertionError("Inventory requested an unsafe operation")
        if path == "/service":
            return self.ids
        return self.details[int(path.split("/")[-1])]


class AccountReconciliationTests(unittest.TestCase):
    def test_no_services_is_verified_zero_not_guessed(self):
        client = FakeAccount([], {})
        report = reconcile_account(client)
        self.assertEqual(report.matching_services, 0)
        self.assertFalse(report.at_limit)
        self.assertEqual(client.calls, [("GET", "/service")])
        self.assertIn("Pending orders", report.summary())

    def test_count_plan_code_only_ignore_unrelated_services(self):
        client = FakeAccount([1, 2, 3], {
            1: service("27skle06-v1"), 2: service("27skle01-v1"),
            3: service("domain-name"),
        })
        report = reconcile_account(client)
        self.assertEqual(report.matching_services, 1)
        self.assertEqual(report.inspected_services, 3)
        self.assertNotIn("27skle01-v1", report.summary())
        self.assertEqual(len(client.calls), 4)

    def test_two_services_or_quantity_two_reach_cap(self):
        self.assertTrue(analyze_services([1, 2], [service(), service()]).at_limit)
        self.assertTrue(analyze_services([1], [service(quantity=2)]).at_limit)
        self.assertIn("LIMIT REACHED", analyze_services([1], [service(quantity=2)]).summary())

    def test_expired_entries_are_conservatively_counted(self):
        old = service()
        old["state"] = "expired"
        self.assertEqual(analyze_services([1], [old]).matching_services, 1)

    def test_incomplete_data_is_never_counted_as_zero(self):
        for ids, details in (([1], []), ([1], [{}]), ([1], [{"plan": {}}]),
                             ([1], [service(quantity=0)]), ([1, 1], [service(), service()]),
                             ([True], [service()])):
            with self.subTest(ids=ids, details=details):
                with self.assertRaises(AccountReconciliationError):
                    analyze_services(ids, details)

    def test_ambiguous_ksle06_variant_fails_closed(self):
        with self.assertRaisesRegex(AccountReconciliationError, "Ambiguous"):
            analyze_services([1], [service("27skle06-v2")])

    def test_non_get_service_mutations_stay_blocked(self):
        client = OVHCartClient("fake-bearer")
        for method, path in (("POST", "/service"), ("PUT", "/service/123"),
                             ("DELETE", "/service/123"), ("GET", "/service/invalid")):
            with self.subTest(method=method, path=path):
                with self.assertRaisesRegex(OrderProbeError, "Blocked unsupported"):
                    client.request(method, path)

    def test_cli_manual_dispatch_never_calls_email(self):
        with patch("account_reconciliation.run_account_reconciliation", return_value=0) as run:
            with patch.object(monitor, "send_mail") as send:
                self.assertEqual(monitor.main(["--reconcile-account"]), 0)
        run.assert_called_once()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
