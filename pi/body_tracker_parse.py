"""YOLOv8 NMS output parsing (no OpenCV / Hailo dependencies)."""

from __future__ import annotations

import numpy as np

_NUM_CLASSES: int = 80
_MAX_DET: int = 100
_VALS_PER_DET: int = 5
_NMS_SHAPE: tuple[int, int, int] = (_NUM_CLASSES, _VALS_PER_DET, _MAX_DET)
_PADDED_CLASS_STRIDE: int = 501
_MIN_BOX_WIDTH: float = 0.06
_MIN_BOX_HEIGHT: float = 0.10
_MIN_BOX_AREA: float = 0.012
_PERSON_CLASS: int = 0


def nms_output_to_tensor(nms_output: np.ndarray) -> np.ndarray | None:
    flat = np.asarray(nms_output, dtype=np.float32).ravel()
    if flat.size == int(np.prod(_NMS_SHAPE)):
        return flat.reshape(_NMS_SHAPE)
    if flat.size == _NUM_CLASSES * _PADDED_CLASS_STRIDE:
        return flat.reshape(_NUM_CLASSES, _PADDED_CLASS_STRIDE)[
            :, : _MAX_DET * _VALS_PER_DET
        ].reshape(_NMS_SHAPE)
    return None


def is_valid_person_box(y0: float, x0: float, y1: float, x1: float, score: float) -> bool:
    if not (0.0 <= score <= 1.0):
        return False
    if not (0.0 <= y0 < y1 <= 1.0 and 0.0 <= x0 < x1 <= 1.0):
        return False
    w = x1 - x0
    h = y1 - y0
    if w < _MIN_BOX_WIDTH or h < _MIN_BOX_HEIGHT:
        return False
    if w * h < _MIN_BOX_AREA:
        return False
    return True


def parse_best_person(
    nms_output: np.ndarray,
    confidence: float = 0.40,
    person_class: int = _PERSON_CLASS,
) -> tuple[float, float, float, float, float] | None:
    nms = nms_output_to_tensor(nms_output)
    if nms is None:
        return None

    best_score = confidence
    best: tuple[float, float, float, float, float] | None = None
    for i in range(_MAX_DET):
        y0, x0, y1, x1, score = (float(v) for v in nms[person_class, :, i])
        if not is_valid_person_box(y0, x0, y1, x1, score):
            continue
        if score >= confidence and score > best_score:
            best_score = score
            best = (y0, x0, y1, x1, score)
    return best
