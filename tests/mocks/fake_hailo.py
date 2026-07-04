"""Fake Hailo NMS output source — returns synthetic numpy arrays."""
from __future__ import annotations

import numpy as np

_NUM_CLASSES = 80
_PADDED_CLASS_STRIDE = 501


def make_empty_nms() -> np.ndarray:
    """Return a (80, 501) flat array with no detections (all zeros)."""
    return np.zeros(_NUM_CLASSES * _PADDED_CLASS_STRIDE, dtype=np.float32)


def make_person_nms(
    y0: float = 0.10,
    x0: float = 0.30,
    y1: float = 0.80,
    x1: float = 0.70,
    score: float = 0.82,
    person_class: int = 0,
) -> np.ndarray:
    """Return a (80*501,) flat array with one person detection.

    Layout per class: [count, y0, x0, y1, x1, score, <padding zeros>]
    """
    mat = np.zeros((_NUM_CLASSES, _PADDED_CLASS_STRIDE), dtype=np.float32)
    mat[person_class, 0] = 1.0          # count = 1
    mat[person_class, 1] = y0
    mat[person_class, 2] = x0
    mat[person_class, 3] = y1
    mat[person_class, 4] = x1
    mat[person_class, 5] = score
    return mat.ravel()


def make_low_score_nms(score: float = 0.35) -> np.ndarray:
    """Return NMS with one detection whose score is below the 0.40 threshold."""
    return make_person_nms(score=score)


def make_tiny_box_nms() -> np.ndarray:
    """Return NMS with a detection whose box is below the minimum size."""
    return make_person_nms(y0=0.40, x0=0.40, y1=0.44, x1=0.42, score=0.90)
