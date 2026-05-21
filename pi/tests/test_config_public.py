"""config_public masking tests."""

import unittest

from config_public import mask_config, public_config


class ConfigPublicTests(unittest.TestCase):
    def test_masks_secret_keys(self) -> None:
        data = {
            "wifi": {"password": "secret123", "ssid": "home"},
            "api_token": "abc",
            "drive": {"max_speed": 255},
        }
        masked = mask_config(data)
        self.assertEqual(masked["wifi"]["password"], "***")
        self.assertEqual(masked["wifi"]["ssid"], "home")
        self.assertEqual(masked["api_token"], "***")
        self.assertEqual(masked["drive"]["max_speed"], 255)

    def test_public_config_does_not_mutate_source(self) -> None:
        src = {"ble_tracker": {"device_mac": "AA:BB"}}
        out = public_config(src)
        out["ble_tracker"]["device_mac"] = "changed"
        self.assertEqual(src["ble_tracker"]["device_mac"], "AA:BB")


if __name__ == "__main__":
    unittest.main()
