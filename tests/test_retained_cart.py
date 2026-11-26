import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock
from urllib.error import HTTPError

from retained_cart import (
    RetainedCartError, persist_retained, read_retained, ensure_no_active_cart,
)
from order_probe import OrderProbeError
from ksle_monitor import PLAN_CODE, REQUIRED_MEMORY, PREFERRED_STORAGE


class RetainedCartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "retained-cart.json"
        self.record = dict(cart_id="cart-123", item_id=1,
                           fqn=f"{PLAN_CODE}.{REQUIRED_MEMORY}.{PREFERRED_STORAGE}",
                           datacenter="sbg", expire="2030-01-01T00:00:00Z")

    def test_persist_private_no_overwrite(self):
        self.assertTrue(persist_retained(self.path, **self.record))
        self.assertEqual(read_retained(self.path)["cart_id"], "cart-123")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(RetainedCartError):
            persist_retained(self.path, **{**self.record, "cart_id": "cart-456"})
        self.assertEqual(read_retained(self.path)["cart_id"], "cart-123")

    def test_existing_active_or_unknown_cart_blocks_new(self):
        persist_retained(self.path, **self.record)
        client = Mock()
        for result in ({"cartId": "cart-123", "expire": "2030-01-01T00:00:00Z"},
                       {"cartId": "cart-123"}, {}, {"cartId": "wrong"}):
            client.request.return_value = result
            with self.assertRaises(RetainedCartError):
                ensure_no_active_cart(self.path, client,
                                      now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertTrue(self.path.exists())

    def test_explicit_expiry_clears_local_only(self):
        persist_retained(self.path, **self.record)
        client = Mock()
        client.request.return_value = {"cartId": "cart-123", "expire": "2025-01-01T00:00:00Z"}
        ensure_no_active_cart(self.path, client, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertFalse(self.path.exists())
        client.request.assert_called_once_with("GET", "/order/cart/cart-123")

    def test_only_explicit_not_found_clears_record(self):
        persist_retained(self.path, **self.record)
        client = Mock()
        def remote(code):
            try:
                raise HTTPError("https://example.invalid", code, "fake", {}, None)
            except HTTPError as cause:
                raise OrderProbeError("fake") from cause
        client.request.side_effect = lambda *args: remote(403)
        with self.assertRaises(RetainedCartError):
            ensure_no_active_cart(self.path, client)
        self.assertTrue(self.path.exists())
        client.request.side_effect = lambda *args: remote(404)
        ensure_no_active_cart(self.path, client)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
