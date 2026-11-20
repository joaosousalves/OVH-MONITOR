"""End-to-end restock rehearsal remains entirely offline and non-billable."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import integration_simulation as simulation
import ksle_monitor as monitor


class IntegrationSimulationTests(unittest.TestCase):
    def test_fallback_ranking_quota_prices_emails_and_deduplication(self):
        output = simulation.simulate_restock_flow()
        self.assertIn("PASS:", output[-1])
        self.assertIn("0 compras", output[-1])
        self.assertTrue(any("guardrails BLOQUEIAM" in message for message in output))

    def test_cli_no_ovh_or_real_smtp_under_any_environment(self):
        with patch.object(monitor, "retrieve_stock", side_effect=AssertionError("network")):
            with patch.object(monitor, "mail_config", side_effect=AssertionError("SMTP config")):
                with patch("builtins.print") as printed:
                    self.assertEqual(monitor.main(["--simulate-restock-flow"]), 0)
        self.assertTrue(any("SIMULATION ONLY" in str(x) for x in printed.call_args_list))


if __name__ == "__main__":
    unittest.main()
