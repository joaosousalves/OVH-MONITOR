"""Pending order inventory is strictly read-only and uses offline API fakes."""
from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ksle_monitor as monitor
import order_probe
from account_reconciliation import AccountInventory, AccountReconciliationError
from pending_orders import (
    PendingOrdersError, count_order_items, reconcile_pending_orders,
    validate_order_ids,
)


def account(_client, maximum):
    return AccountInventory(0, 15, maximum, inspected_hardware=2)


def detail(plan, *, quantity="1", domain="*001", detail_id=5, cart_id=1):
    return (
        {"orderDetailId": detail_id, "quantity": quantity,
         "domain": domain, "cartItemID": cart_id},
        {"order": {"plan": {"code": plan}, "configurations": []}},
    )


class FakeOrders:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def request(self, method, path):
        self.calls.append((method, path))
        if method != "GET":
            raise AssertionError("A write would be an unsafe operation")
        reply = self.responses.get(path)
        if isinstance(reply, Exception):
            raise reply
        if path not in self.responses:
            raise AssertionError(f"Unexpected GET: {path}")
        return reply


def one_order(*, status="checking", code="27skle06-v1", order_id=101,
              expiration="2099-12-31"):
    base = f"/me/order/{order_id}"
    main, extension = detail(code)
    return {
        "/me/order": [order_id],
        base: {"orderId": order_id, "expirationDate": expiration},
        base + "/status": status,
        base + "/details": [5],
        base + "/details/5": main,
        base + "/details/5/extension": extension,
    }


class PendingOrderTests(unittest.TestCase):
    def test_empty_order_list_and_no_services_is_verified_zero(self):
        fake = FakeOrders({"/me/order": []})
        result = reconcile_pending_orders(fake, service_check=account)
        self.assertEqual((result.inspected_orders, result.pending_servers), (0, 0))
        self.assertIn("0/2", result.summary())
        self.assertIn("Checkout/payment remain disabled", result.summary())
        self.assertEqual(fake.calls, [("GET", "/me/order")])

    def test_historical_delivered_and_cancelled_not_counted_twice(self):
        fake = FakeOrders({
            "/me/order": [101, 102],
            "/me/order/101/status": "delivered",
            "/me/order/102/status": "cancelled",
        })
        result = reconcile_pending_orders(fake, service_check=account)
        self.assertEqual(result.pending_servers, 0)
        self.assertEqual(result.open_orders, 0)
        self.assertEqual(len(fake.calls), 3)

    def test_pending_matching_order_is_counted(self):
        for status in ("notPaid", "checking", "delivering", "documentsRequested", "cancelling"):
            with self.subTest(status=status):
                fake = FakeOrders(one_order(status=status))
                result = reconcile_pending_orders(fake, service_check=account)
                self.assertEqual(result.pending_servers, 1)
                self.assertEqual(result.open_orders, 1)
                self.assertFalse(result.overlap_unverified)
                self.assertEqual(result.upper_bound, 1)
                self.assertTrue(all(method == "GET" for method, _ in fake.calls))

    def test_expired_not_paid_skips_unreadable_details_and_is_not_open(self):
        responses = one_order(status="notPaid", expiration="2020-01-01", code=None)
        for suffix in ("/details", "/details/5", "/details/5/extension"):
            del responses["/me/order/101" + suffix]
        fake = FakeOrders(responses)
        result = reconcile_pending_orders(
            fake, service_check=account, as_of_utc_date=date(2026, 11, 20),
        )
        self.assertEqual((result.open_orders, result.pending_servers,
                          result.expired_unpaid_orders), (0, 0, 1))
        self.assertIn("1 expired unpaid order(s) excluded", result.summary())
        self.assertEqual(fake.calls, [
            ("GET", "/me/order"),
            ("GET", "/me/order/101/status"),
            ("GET", "/me/order/101"),
        ])

    def test_unpaid_expiring_today_or_later_is_still_open(self):
        for expiration in ("2026-11-20", "2026-11-21"):
            with self.subTest(expiration=expiration):
                fake = FakeOrders(one_order(status="notPaid", expiration=expiration))
                result = reconcile_pending_orders(
                    fake, service_check=account, as_of_utc_date=date(2026, 11, 20),
                )
                self.assertEqual(result.pending_servers, 1)
                self.assertEqual(result.expired_unpaid_orders, 0)
                self.assertIn(("GET", "/me/order/101/details"), fake.calls)

    def test_timezone_aware_expiration_uses_utc_calendar_day(self):
        for expiration, expected_expired in (
            ("2026-11-19T23:00:00Z", True),
            ("2026-11-20T01:00:00+02:00", True),
            ("2026-11-19T23:00:00-02:00", False),
        ):
            with self.subTest(expiration=expiration):
                fake = FakeOrders(one_order(status="notPaid", expiration=expiration))
                result = reconcile_pending_orders(
                    fake, service_check=account, as_of_utc_date=date(2026, 11, 20),
                )
                self.assertEqual(result.expired_unpaid_orders, int(expected_expired))
                self.assertEqual(result.pending_servers, int(not expected_expired))

    def test_unknown_unpaid_expiration_fails_closed_before_details(self):
        for expiration in (None, "", "not-a-date", "2026-02-30",
                           "2026-11-19T14:30:00", 12345):
            with self.subTest(expiration=expiration):
                fake = FakeOrders(one_order(status="notPaid", expiration=expiration))
                with self.assertRaisesRegex(PendingOrdersError, "expiration date"):
                    reconcile_pending_orders(
                        fake, service_check=account, as_of_utc_date=date(2026, 11, 20),
                    )
                self.assertEqual(len(fake.calls), 3)

    def test_unpaid_order_metadata_id_mismatch_fails_closed(self):
        responses = one_order(status="notPaid")
        responses["/me/order/101"]["orderId"] = 102
        with self.assertRaisesRegex(PendingOrdersError, "Mismatched unpaid order"):
            reconcile_pending_orders(FakeOrders(responses), service_check=account)

    def test_non_unpaid_status_does_not_need_order_expiration_metadata(self):
        responses = one_order(status="checking")
        del responses["/me/order/101"]
        fake = FakeOrders(responses)
        result = reconcile_pending_orders(fake, service_check=account)
        self.assertEqual(result.pending_servers, 1)
        self.assertNotIn(("GET", "/me/order/101"), fake.calls)

    def test_expiration_lookup_403_fails_closed_without_leaking_order_id(self):
        responses = one_order(status="notPaid")
        responses["/me/order/101"] = order_probe.OrderProbeError(
            "OVHcloud API 403 /me/order/101"
        )
        with self.assertRaises(PendingOrdersError) as context:
            reconcile_pending_orders(FakeOrders(responses), service_check=account)
        self.assertNotIn("101", str(context.exception))
        self.assertIn("GET API rights", str(context.exception))

    def test_two_different_pending_ksle6_orders_reach_cap(self):
        responses = one_order(order_id=101)
        responses.update({k: v for k, v in one_order(order_id=102).items() if k != "/me/order"})
        responses["/me/order"] = [101, 102]
        result = reconcile_pending_orders(FakeOrders(responses), service_check=account)
        self.assertEqual(result.pending_servers, 2)
        self.assertIn("LIMIT REACHED", result.summary())

    def test_unrelated_plan_does_not_consume_quota(self):
        result = reconcile_pending_orders(FakeOrders(one_order(code="other-plan")), service_check=account)
        self.assertEqual(result.pending_servers, 0)
        self.assertEqual(result.open_orders, 1)

    def test_duplicate_invoice_lines_deduplicated_by_root_domain(self):
        self.assertEqual(count_order_items([
            detail("27skle06-v1", domain="*001", detail_id=5),
            detail("27skle06-v1", domain="*001.001", detail_id=6),
            detail("other-plan", domain="*001.002", detail_id=7),
        ]), 1)
        self.assertEqual(count_order_items([
            detail("27skle06-v1", quantity="2", domain="*001"),
            detail("27skle06-v1", quantity="2", domain="*001.001"),
        ]), 2)

    def test_qty_conflict_and_distinct_cart_items_under_same_root_fail_closed(self):
        with self.assertRaisesRegex(PendingOrdersError, "Conflicting"):
            count_order_items([
                detail("27skle06-v1", quantity="1", domain="*001"),
                detail("27skle06-v1", quantity="2", domain="*001.001"),
            ])
        with self.assertRaisesRegex(PendingOrdersError, "Conflicting"):
            count_order_items([
                detail("27skle06-v1", cart_id=1, domain="*001"),
                detail("27skle06-v1", cart_id=2, domain="*001.001"),
            ])

    def test_service_and_open_order_overlap_is_not_falsely_added_as_distinct(self):
        def one_service(_client, maximum):
            return AccountInventory(1, 15, maximum)
        result = reconcile_pending_orders(FakeOrders(one_order()), service_check=one_service)
        self.assertTrue(result.overlap_unverified)
        self.assertEqual((result.lower_bound, result.upper_bound), (1, 2))
        self.assertIn("INCONCLUSIVE OVERLAP", result.summary())
        self.assertNotIn("Verified service + open-order count", result.summary())

    def test_unknown_status_fails_before_inspecting_details(self):
        for status in ("unknown", "newState", None, {}, 3):
            with self.subTest(status=status):
                fake = FakeOrders({"/me/order": [101], "/me/order/101/status": status})
                with self.assertRaises(PendingOrdersError):
                    reconcile_pending_orders(fake, service_check=account)
                self.assertEqual(len(fake.calls), 2)

    def test_invalid_and_oversized_order_lists_fail_closed(self):
        for ids in (None, {}, [0], [True], ["123"], [1, 1], list(range(1, 252))):
            with self.subTest(ids=type(ids).__name__):
                with self.assertRaises(PendingOrdersError):
                    validate_order_ids(ids)

    def test_missing_detail_extension_and_unknown_model_fail_closed(self):
        cases = (
            None, {}, {"order": {"plan": {}}},
            {"order": {"plan": {"code": "27skle06-v2"}}},
        )
        for extension in cases:
            with self.subTest(extension=extension):
                with self.assertRaises(PendingOrdersError):
                    count_order_items([(detail("27skle06-v1")[0], extension)])

    def test_403_get_failure_hides_order_id_in_exception(self):
        responses = one_order()
        responses["/me/order/101/details"] = order_probe.OrderProbeError(
            "OVHcloud API 403 /me/order/101/details"
        )
        with self.assertRaises(PendingOrdersError) as context:
            reconcile_pending_orders(FakeOrders(responses), service_check=account)
        self.assertNotIn("101", str(context.exception))
        self.assertIn("GET API rights", str(context.exception))

    def test_service_scan_failure_blocks_before_order_list(self):
        fake = FakeOrders({"/me/order": [101]})
        def error(_client, _maximum):
            raise AccountReconciliationError("inconclusive")
        with self.assertRaises(AccountReconciliationError):
            reconcile_pending_orders(fake, service_check=error)
        self.assertEqual(fake.calls, [])

    def test_order_details_get_allowed_but_checkout_and_mutations_still_blocked(self):
        client = order_probe.OVHCartClient("test-token")
        paths = (
            "/me/order/101/details",
            "/me/order/101/details/5",
            "/me/order/101/details/5/extension",
        )
        with patch.object(order_probe, "urlopen", side_effect=lambda *_a, **_kw: BytesIO(b"[]")) as web:
            for path in paths:
                self.assertEqual(client.request("GET", path), [])
            self.assertEqual(web.call_count, 3)
        with patch.object(order_probe, "urlopen") as web:
            for method, path in (
                ("POST", "/me/order/101/pay"),
                ("POST", "/order/cart/abc-123/checkout"),
                ("PUT", "/me/order/101/details/5"),
                ("GET", "/me/order/101/details/bad/extension"),
            ):
                with self.assertRaisesRegex(order_probe.OrderProbeError, "Blocked unsupported"):
                    client.request(method, path)
            web.assert_not_called()

    def test_cli_dispatch_does_not_send_email(self):
        with patch("pending_orders.run_pending_order_reconciliation", return_value=0) as run:
            with patch.object(monitor, "send_mail") as send_mail:
                self.assertEqual(monitor.main(["--reconcile-pending-orders"]), 0)
        run.assert_called_once()
        send_mail.assert_not_called()


if __name__ == "__main__":
    unittest.main()
