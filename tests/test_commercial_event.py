import tempfile
import unittest
from pathlib import Path

from ksle_monitor import (
    PLAN_CODE, REQUIRED_MEMORY, PREFERRED_STORAGE, Stock,
    load_alerted, publish_commercial_event,
)


class CommercialEventTests(unittest.TestCase):
    def test_event_edges_and_missing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "commercial-event.json"
            fqn = f"{PLAN_CODE}.{REQUIRED_MEMORY}.{PREFERRED_STORAGE}"
            available = Stock(fqn, PREFERRED_STORAGE, "sbg", "1H-high")
            unavailable = Stock(fqn, PREFERRED_STORAGE, "sbg", "unavailable")
            self.assertFalse(publish_commercial_event([unavailable], path))
            self.assertTrue(publish_commercial_event([available], path))
            self.assertEqual(load_alerted(path), {available.key})
            self.assertFalse(publish_commercial_event([available], path))
            self.assertFalse(publish_commercial_event([], path))
            self.assertTrue(publish_commercial_event([unavailable], path))
            self.assertEqual(load_alerted(path), set())
            self.assertTrue(publish_commercial_event([available], path))

    def test_ineligible_product_does_not_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "commercial-event.json"
            stock = Stock("wrong-plan", PREFERRED_STORAGE, "sbg", "1H-high")
            self.assertFalse(publish_commercial_event([stock], path))
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
