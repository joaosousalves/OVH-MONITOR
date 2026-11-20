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


def service_without_code(path, name=None, quantity=1):
    return {
        "plan": {"code": None, "product": {"name": None}},
        "quantity": quantity,
        "route": {
            "path": path,
            "vars": [{"key": "serviceName", "value": name}] if name else [],
        },
        "resource": {"name": name} if name else {},
        "state": "ok",
    }


class FakeAccount:
    def __init__(self, ids, details, hardware=None):
        self.ids, self.details, self.hardware, self.calls = ids, details, hardware or {}, []

    def request(self, method, path):
        self.calls.append((method, path))
        if method != "GET":
            raise AssertionError("Inventory requested an unsafe operation")
        if path == "/service":
            return self.ids
        if path.startswith("/dedicated/server/"):
            return self.hardware[path]
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

    def test_real_world_missing_plan_codes_two_non_matching_dedicated_servers(self):
        routes = (["/ip/service"] * 4 + ["/email/domain"] * 2
                  + ["/domain/{serviceName}"] * 2 + ["/domain/zone"] * 2
                  + ["/vrack/{serviceName}", "/cloud/project", "/vps/{serviceName}"])
        details = {i: service_without_code(path) for i, path in enumerate(routes, 1)}
        details[14] = service_without_code(
            "/dedicated/server/{serviceName}", "ns100.ip-192-0-2.eu")
        details[15] = service_without_code(
            "/dedicated/server/{serviceName}", "ns200.ip-198-51-100.net")
        hardware = {
            "/dedicated/server/ns100.ip-192-0-2.eu/specifications/hardware": {
                "description": "KS-6 | AMD Epyc 7351P - AMD Epyc 7351p",
                "memorySize": {"value": 131072, "unit": "MB"},
            },
            "/dedicated/server/ns200.ip-198-51-100.net/specifications/hardware": {
                "description": "KS-LE-5 - Intel Xeon E5-1650v4",
                "memorySize": {"value": 65536, "unit": "MB"},
            },
        }
        client = FakeAccount(list(details), details, hardware)
        report = reconcile_account(client)
        self.assertEqual((report.matching_services, report.inspected_services), (0, 15))
        self.assertEqual(report.inspected_hardware, 2)
        self.assertEqual(len(client.calls), 18)
        self.assertTrue(all(method == "GET" for method, _ in client.calls))
        self.assertNotIn("ns100", report.summary())

    def test_hardware_identifies_one_or_two_ksle6_servers_without_plan_codes(self):
        names = ("ns100.ip-192-0-2.eu", "ns200.ip-198-51-100.net")
        details = {
            i: service_without_code("/dedicated/server/{serviceName}", name)
            for i, name in enumerate(names, 1)
        }
        hardware = {
            f"/dedicated/server/{name}/specifications/hardware": {
                "description": f"KS-LE-{6 if i == 1 else 5} - CPU model"
            }
            for i, name in enumerate(names, 1)
        }
        client = FakeAccount([1, 2], details, hardware)
        self.assertEqual(reconcile_account(client).matching_services, 1)
        hardware[f"/dedicated/server/{names[1]}/specifications/hardware"] = {
            "description": "KS-LE-6 | AMD EPYC"
        }
        self.assertTrue(reconcile_account(FakeAccount([1, 2], details, hardware)).at_limit)

    def test_count_known_plan_code_and_missing_code_hardware_together(self):
        details = {
            1: service(),
            2: service_without_code("/dedicated/server/{serviceName}", "ns200.ip-198-51-100.net"),
        }
        hardware = {
            "/dedicated/server/ns200.ip-198-51-100.net/specifications/hardware": {
                "description": "KS-LE-6 | AMD EPYC"
            }
        }
        self.assertEqual(reconcile_account(FakeAccount([1, 2], details, hardware)).matching_services, 2)

    def test_missing_hardware_data_or_unknown_route_never_means_zero(self):
        missing = service_without_code("/dedicated/server/{serviceName}", "ns100.ip-192-0-2.eu")
        with self.assertRaisesRegex(AccountReconciliationError, "Missing dedicated server hardware"):
            analyze_services([1], [missing])
        with self.assertRaisesRegex(AccountReconciliationError, "route"):
            analyze_services([1], [service(code=None)])
        with self.assertRaisesRegex(AccountReconciliationError, "commercial model"):
            analyze_services([1], [missing], hardware_by_service_id={1: {}})
        with self.assertRaisesRegex(AccountReconciliationError, "Ambiguous"):
            analyze_services([1], [missing], hardware_by_service_id={1: {"description": "KSLE6 new"}})
        with self.assertRaisesRegex(AccountReconciliationError, "Unrecognized dedicated"):
            analyze_services([1], [service_without_code("/dedicated/server/other")])

    def test_empty_plan_code_also_uses_hardware_fallback(self):
        name = "ns100.ip-192-0-2.eu"
        detail = service_without_code("/dedicated/server/{serviceName}", name)
        detail["plan"]["code"] = ""
        hardware = {
            f"/dedicated/server/{name}/specifications/hardware": {"description": "KS-LE-6 - CPU"}
        }
        self.assertEqual(reconcile_account(FakeAccount([1], {1: detail}, hardware)).matching_services, 1)

    def test_hardware_get_failure_is_fail_closed_and_does_not_log_hostnames(self):
        class ForbiddenAccount(FakeAccount):
            def request(self, method, path):
                if "specifications/hardware" in path:
                    raise OrderProbeError("OVHcloud API 403 on GET " + path)
                return super().request(method, path)

        name = "ns100.ip-192-0-2.eu"
        details = {1: service_without_code("/dedicated/server/{serviceName}", name)}
        with self.assertRaises(AccountReconciliationError) as ctx:
            reconcile_account(ForbiddenAccount([1], details))
        self.assertNotIn(name, str(ctx.exception))
        self.assertIn("permission", str(ctx.exception))

    def test_server_name_or_route_ambiguity_blocks_remote_requests(self):
        invalid_names = ("../other", "ns100/a", "ns1?query", "", "a" * 254)
        for name in invalid_names:
            with self.subTest(name=name):
                detail = service_without_code("/dedicated/server/{serviceName}", name)
                client = FakeAccount([1], {1: detail})
                with self.assertRaises(AccountReconciliationError):
                    reconcile_account(client)
                self.assertEqual(len(client.calls), 2)

    def test_route_variable_identifies_server_even_if_resource_has_a_display_alias(self):
        name = "ns100.ip-192-0-2.eu"
        detail = service_without_code("/dedicated/server/{serviceName}", name)
        detail["resource"]["name"] = "account-display-alias"
        hardware = {
            f"/dedicated/server/{name}/specifications/hardware": {"description": "KS-LE-5 - CPU"}
        }
        self.assertEqual(reconcile_account(FakeAccount([1], {1: detail}, hardware)).matching_services, 0)

    def test_non_get_service_mutations_stay_blocked(self):
        client = OVHCartClient("fake-bearer")
        for method, path in (("POST", "/service"), ("PUT", "/service/123"),
                             ("DELETE", "/service/123"), ("GET", "/service/invalid")):
            with self.subTest(method=method, path=path):
                with self.assertRaisesRegex(OrderProbeError, "Blocked unsupported"):
                    client.request(method, path)

    def test_hardware_specifications_read_only_is_allowlisted(self):
        client = OVHCartClient("fake-bearer")
        path = "/dedicated/server/ns1234567.ip-192-0-2.eu/specifications/hardware"
        fake_reply = {"description": "KS-LE-6", "memorySize": {"value": 128, "unit": "GB"}}
        from io import BytesIO
        import json
        with patch("order_probe.urlopen", return_value=BytesIO(json.dumps(fake_reply).encode())) as open_url:
            self.assertEqual(client.request("GET", path), fake_reply)
        self.assertEqual(open_url.call_count, 1)

    def test_hardware_specifications_mutations_and_other_paths_blocked(self):
        client = OVHCartClient("fake-bearer")
        valid_path = "/dedicated/server/ns1234567.ip-192-0-2.eu/specifications/hardware"
        with patch("order_probe.urlopen") as open_url:
            for method, path in (
                ("POST", valid_path),
                ("PUT", valid_path),
                ("DELETE", valid_path),
                ("GET", "/dedicated/server/ns1234567.ip-192-0-2.eu"),
                ("GET", "/dedicated/server/../specifications/hardware"),
                ("GET", "/dedicated/server/ns1234567.ip-192-0-2.eu/reboot"),
            ):
                with self.subTest(method=method, path=path):
                    with self.assertRaisesRegex(OrderProbeError, "Blocked unsupported"):
                        client.request(method, path)
            open_url.assert_not_called()

    def test_cli_manual_dispatch_never_calls_email(self):
        with patch("account_reconciliation.run_account_reconciliation", return_value=0) as run:
            with patch.object(monitor, "send_mail") as send:
                self.assertEqual(monitor.main(["--reconcile-account"]), 0)
        run.assert_called_once()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
