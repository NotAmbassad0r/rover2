"""config_store tuning validation."""

import unittest

from config_store import extract_tuning_patch, merge_tuning


class ConfigStoreTests(unittest.TestCase):
    def test_extract_body_and_safety(self) -> None:
        patch = extract_tuning_patch({
            "body_tracker": {"turn_speed": 90, "confidence": 0.35},
            "safety": {"safe_distance_cm": 35},
        })
        self.assertEqual(patch["body_tracker"]["turn_speed"], 90)
        self.assertEqual(patch["safety"]["safe_distance_cm"], 35)

    def test_clamps_speed(self) -> None:
        patch = extract_tuning_patch({"body_tracker": {"turn_speed": 999}})
        self.assertEqual(patch["body_tracker"]["turn_speed"], 255)

    def test_rejects_bad_avoid(self) -> None:
        with self.assertRaises(ValueError):
            extract_tuning_patch({"body_tracker": {"avoid_default": "up"}})

    def test_merge(self) -> None:
        base = {"body_tracker": {"turn_speed": 100, "confidence": 0.4}}
        merged = merge_tuning(base, {"body_tracker": {"turn_speed": 80}})
        self.assertEqual(merged["body_tracker"]["turn_speed"], 80)
        self.assertEqual(merged["body_tracker"]["confidence"], 0.4)

    def test_arm_invert_bool(self) -> None:
        patch = extract_tuning_patch({"arm": {"invert": True}})
        self.assertTrue(patch["arm"]["invert"])


if __name__ == "__main__":
    unittest.main()
