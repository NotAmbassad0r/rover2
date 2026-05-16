"""Unit tests for Hailo YOLOv8 NMS parsing in body_tracker."""

from __future__ import annotations

import unittest

import numpy as np

from body_tracker_parse import is_valid_person_box, nms_output_to_tensor, parse_best_person


class ParseBestPersonTests(unittest.TestCase):
    def _set_person_det(self, tensor: np.ndarray, slot: int, box: list[float]) -> None:
        tensor[0, :, slot] = box

    def test_padded_40080_layout_picks_highest_score(self) -> None:
        flat = np.zeros(80 * 501, dtype=np.float32)
        tensor = nms_output_to_tensor(flat)
        assert tensor is not None
        self._set_person_det(tensor, 1, [0.1, 0.35, 0.9, 0.55, 0.92])
        self._set_person_det(tensor, 0, [0.1, 0.2, 0.9, 0.4, 0.55])
        best = parse_best_person(flat, confidence=0.4)
        self.assertIsNotNone(best)
        y0, x0, y1, x1, score = best  # type: ignore[misc]
        self.assertAlmostEqual(score, 0.92)
        self.assertAlmostEqual(x0, 0.35)
        self.assertAlmostEqual(x1, 0.55)

    def test_nms_tensor_layout(self) -> None:
        nms = np.zeros((80, 5, 100), dtype=np.float32)
        self._set_person_det(nms, 3, [0.0, 0.25, 1.0, 0.45, 0.88])
        best = parse_best_person(nms, confidence=0.4)
        self.assertIsNotNone(best)
        self.assertAlmostEqual(best[4], 0.88)  # type: ignore[index]

    def test_below_threshold_returns_none(self) -> None:
        flat = np.zeros(80 * 501, dtype=np.float32)
        tensor = nms_output_to_tensor(flat)
        assert tensor is not None
        self._set_person_det(tensor, 0, [0.1, 0.2, 0.5, 0.3, 0.2])
        self.assertIsNone(parse_best_person(flat, confidence=0.4))

    def test_sequential_stride_is_not_used(self) -> None:
        flat = np.zeros(80 * 501, dtype=np.float32)
        flat[0:5] = [0.1, 0.3, 0.9, 0.5, 0.85]
        self.assertIsNone(parse_best_person(flat, confidence=0.4))

    def test_invalid_box_rejected(self) -> None:
        self.assertFalse(is_valid_person_box(0.0, 0.4, 1.0, 0.41, 0.9))
        self.assertTrue(is_valid_person_box(0.1, 0.3, 0.9, 0.5, 0.9))


if __name__ == "__main__":
    unittest.main()
