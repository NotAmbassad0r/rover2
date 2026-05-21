"""Diagnostics catalog and report shape."""

import unittest
from unittest.mock import MagicMock

from diagnostics import SCRIPT_CATALOG, gather_diagnostics


class DiagnosticsCatalogTests(unittest.TestCase):
    def test_catalog_has_required_fields(self) -> None:
        self.assertGreaterEqual(len(SCRIPT_CATALOG), 3)
        for entry in SCRIPT_CATALOG:
            self.assertIn("id", entry)
            self.assertIn("title", entry)
            self.assertIn("where", entry)

    def test_wifi_restore_is_pi_api(self) -> None:
        wifi = next(s for s in SCRIPT_CATALOG if s["id"] == "restore_wifi")
        self.assertEqual(wifi["where"], "pi_api")
        self.assertIn("wifi-restore", wifi["api"])


class GatherDiagnosticsTests(unittest.TestCase):
    def test_report_sections(self) -> None:
        megapi = MagicMock()
        megapi.connected = False
        megapi.port = "/dev/ttyUSB0"
        megapi.firmware_version = None
        megapi.motors_ready = False
        megapi.request_ultrasonic.return_value = None

        report = gather_diagnostics(megapi, api_port=8082)
        self.assertIn("timestamp", report)
        for key in ("hardware", "network", "software", "system"):
            self.assertIn(key, report)
            self.assertIsInstance(report[key], dict)


if __name__ == "__main__":
    unittest.main()
