"""Tests for pi/body_tracker_parse.py — pure NMS parsing, no hardware."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "pi"))

from body_tracker_parse import (
    _MAX_DET,
    _MIN_BOX_AREA,
    _MIN_BOX_HEIGHT,
    _MIN_BOX_WIDTH,
    _NUM_CLASSES,
    _PADDED_CLASS_STRIDE,
    _VALS_PER_DET,
    is_valid_person_box,
    nms_output_to_tensor,
    parse_best_person,
)
from tests.mocks.fake_hailo import (
    make_empty_nms,
    make_low_score_nms,
    make_person_nms,
    make_tiny_box_nms,
)

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "nms_live_sample.npy"

PERSON_CLASS = 0


# ── nms_output_to_tensor ────────────────────────────────────────────────────

class TestNmsOutputToTensor:
    def test_padded_format_accepted(self):
        flat = make_empty_nms()
        assert flat.size == _NUM_CLASSES * _PADDED_CLASS_STRIDE
        tensor = nms_output_to_tensor(flat)
        assert tensor is not None
        assert tensor.shape == (_NUM_CLASSES, _MAX_DET, _VALS_PER_DET)

    def test_dense_format_accepted(self):
        flat = np.zeros(_NUM_CLASSES * _MAX_DET * _VALS_PER_DET, dtype=np.float32)
        tensor = nms_output_to_tensor(flat)
        assert tensor is not None
        assert tensor.shape == (_NUM_CLASSES, _MAX_DET, _VALS_PER_DET)

    def test_unknown_size_returns_none(self):
        flat = np.zeros(999, dtype=np.float32)
        assert nms_output_to_tensor(flat) is None

    def test_count_at_col0_is_excluded(self):
        """Col 0 is the per-class count — must NOT appear in the detection slice."""
        mat = np.zeros((_NUM_CLASSES, _PADDED_CLASS_STRIDE), dtype=np.float32)
        # Write a sentinel (9.9) into col 0 (count) for person class
        mat[PERSON_CLASS, 0] = 9.9
        # Write a known score into col 5 (first detection's score slot after count)
        mat[PERSON_CLASS, 5] = 0.75
        tensor = nms_output_to_tensor(mat.ravel())
        assert tensor is not None
        # detection [0, 0, :] should be [col1, col2, col3, col4, col5]
        # i.e. [0, 0, 0, 0, 0.75] — the count (9.9) must NOT appear
        vals = [float(v) for v in tensor[PERSON_CLASS, 0, :]]
        assert 9.9 not in vals, "count at col0 leaked into detection data"
        assert abs(vals[4] - 0.75) < 1e-5, "score at col5 should be last field"


# ── is_valid_person_box ─────────────────────────────────────────────────────

class TestIsValidPersonBox:
    def test_valid_box(self):
        assert is_valid_person_box(0.1, 0.3, 0.8, 0.7, 0.82)

    def test_negative_score_rejected(self):
        assert not is_valid_person_box(0.1, 0.3, 0.8, 0.7, -0.1)

    def test_inverted_coords_rejected(self):
        assert not is_valid_person_box(0.8, 0.3, 0.1, 0.7, 0.82)  # y0 > y1

    def test_out_of_bounds_rejected(self):
        assert not is_valid_person_box(0.1, 0.3, 1.10, 0.7, 0.82)  # y1 > 1.05

    def test_too_narrow_rejected(self):
        w = _MIN_BOX_WIDTH - 0.001
        assert not is_valid_person_box(0.1, 0.4, 0.9, 0.4 + w, 0.82)

    def test_too_short_rejected(self):
        h = _MIN_BOX_HEIGHT - 0.001
        assert not is_valid_person_box(0.4, 0.2, 0.4 + h, 0.8, 0.82)

    def test_too_small_area_rejected(self):
        # Build a box that passes width and height gates but fails area
        # width=0.04 height=0.07 area=0.0028 < 0.003
        assert not is_valid_person_box(0.10, 0.20, 0.17, 0.24, 0.82)


# ── parse_best_person ───────────────────────────────────────────────────────

class TestParseBestPerson:
    def test_empty_scene_returns_none(self):
        flat = make_empty_nms()
        result = parse_best_person(flat)
        assert result is None

    def test_synthetic_person_detected(self):
        """Core regression: correct field order and count-drop fix."""
        flat = make_person_nms(y0=0.10, x0=0.30, y1=0.80, x1=0.70, score=0.82)
        result = parse_best_person(flat)
        assert result is not None, "person should be detected"
        y0, x0, y1, x1, score = result
        assert abs(y0 - 0.10) < 1e-4, f"y0 expected 0.10 got {y0}"
        assert abs(x0 - 0.30) < 1e-4, f"x0 expected 0.30 got {x0}"
        assert abs(y1 - 0.80) < 1e-4, f"y1 expected 0.80 got {y1}"
        assert abs(x1 - 0.70) < 1e-4, f"x1 expected 0.70 got {x1}"
        assert abs(score - 0.82) < 1e-4, f"score expected 0.82 got {score}"

    def test_score_is_realistic_not_one(self):
        """Regression: before fix, score was always 1.00 due to wrong field order."""
        flat = make_person_nms(score=0.82)
        result = parse_best_person(flat)
        assert result is not None
        score = result[4]
        assert score < 0.999, f"score should not be 1.0; was {score}"
        assert score > 0.4,   f"score should be > 0.4 confidence; was {score}"

    def test_score_below_confidence_threshold_rejected(self):
        flat = make_low_score_nms(score=0.35)
        result = parse_best_person(flat, confidence=0.40)
        assert result is None, "score 0.35 < 0.40 should be rejected"

    def test_score_at_confidence_threshold_accepted(self):
        flat = make_person_nms(score=0.40)
        result = parse_best_person(flat, confidence=0.40)
        assert result is not None

    def test_tiny_box_rejected(self):
        flat = make_tiny_box_nms()
        result = parse_best_person(flat)
        assert result is None, "tiny box should fail size gates"

    def test_coord_swap_normalised(self):
        """x0 > x1 is swapped and the result is still valid."""
        # Give x0 > x1 deliberately; the parser should swap and return a valid box
        flat = make_person_nms(y0=0.1, x0=0.70, y1=0.8, x1=0.30, score=0.82)
        result = parse_best_person(flat)
        assert result is not None, "swapped coords should be normalised and accepted"
        _, rx0, _, rx1, _ = result
        assert rx0 < rx1, "normalised x0 should be less than x1"

    def test_best_of_multiple_detections(self):
        """When two detections are present, the higher-score one wins."""
        mat = np.zeros((_NUM_CLASSES, _PADDED_CLASS_STRIDE), dtype=np.float32)
        # Detection 0: score 0.55
        mat[PERSON_CLASS, 0] = 2.0        # count
        mat[PERSON_CLASS, 1] = 0.10
        mat[PERSON_CLASS, 2] = 0.30
        mat[PERSON_CLASS, 3] = 0.80
        mat[PERSON_CLASS, 4] = 0.70
        mat[PERSON_CLASS, 5] = 0.55
        # Detection 1: score 0.88 (larger bbox, should win)
        mat[PERSON_CLASS, 6] = 0.05
        mat[PERSON_CLASS, 7] = 0.10
        mat[PERSON_CLASS, 8] = 0.95
        mat[PERSON_CLASS, 9] = 0.90
        mat[PERSON_CLASS, 10] = 0.88
        result = parse_best_person(mat.ravel())
        assert result is not None
        assert abs(result[4] - 0.88) < 1e-4, "highest-score detection should win"

    def test_non_person_class_ignored(self):
        """Detections in other classes must not be returned as persons."""
        mat = np.zeros((_NUM_CLASSES, _PADDED_CLASS_STRIDE), dtype=np.float32)
        # Put a high-score detection in class 1 (bicycle), not class 0 (person)
        mat[1, 0] = 1.0
        mat[1, 1] = 0.10
        mat[1, 2] = 0.30
        mat[1, 3] = 0.80
        mat[1, 4] = 0.70
        mat[1, 5] = 0.95
        result = parse_best_person(mat.ravel())
        assert result is None, "non-person class detection should not be returned"


# ── Fixture-based test (requires real captured data) ────────────────────────

@pytest.mark.skipif(
    not _FIXTURE_PATH.exists(),
    reason=(
        "NMS fixture absent — capture on Pi with: "
        "python3 /opt/rover2/capture_nms_fixture.py"
    ),
)
def test_live_fixture_parses():
    nms_raw = np.load(_FIXTURE_PATH)
    result = parse_best_person(nms_raw)
    if result is not None:
        y0, x0, y1, x1, score = result
        assert 0.0 <= y0 < y1 <= 1.05
        assert 0.0 <= x0 < x1 <= 1.05
        assert 0.40 <= score < 1.0, f"live fixture score out of range: {score}"
