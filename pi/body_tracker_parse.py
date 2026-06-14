"""YOLOv8 NMS output parsing (no OpenCV / Hailo dependencies)."""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)

_NUM_CLASSES: int = 80
_MAX_DET: int = 100
_VALS_PER_DET: int = 5
_NMS_SHAPE: tuple[int, int, int] = (_NUM_CLASSES, _MAX_DET, _VALS_PER_DET)
_PADDED_CLASS_STRIDE: int = 501
_MIN_BOX_WIDTH: float = 0.03   # was 0.06 — reduced to detect at 3-4m distance
_MIN_BOX_HEIGHT: float = 0.06  # was 0.10 — reduced to detect at 3-4m distance
_MIN_BOX_AREA: float = 0.003   # was 0.012 — reduced to detect at 3-4m distance
_PERSON_CLASS: int = 0


def nms_output_to_tensor(nms_output: np.ndarray) -> np.ndarray | None:
    flat = np.asarray(nms_output, dtype=np.float32).ravel()
    if flat.size == int(np.prod(_NMS_SHAPE)):
        return flat.reshape(_NMS_SHAPE)
    if flat.size == _NUM_CLASSES * _PADDED_CLASS_STRIDE:
        mat = flat.reshape(_NUM_CLASSES, _PADDED_CLASS_STRIDE)
        class_maxes = [(i, float(v)) for i, v in enumerate(mat.max(axis=1)) if v > 0.1]
        logger.debug("nms classes with max>0.1: %s", class_maxes)
        nz = np.where(flat > 0.1)[0]
        logger.debug("nms nonzero[:10] idx=%s val=%s", nz[:10].tolist(), flat[nz[:10]].tolist())
        return mat[:, : _MAX_DET * _VALS_PER_DET].reshape(_NMS_SHAPE)
    return None


def is_valid_person_box(y0: float, x0: float, y1: float, x1: float, score: float) -> bool:
    if score < 0.0:
        return False
    if not (0.0 <= y0 < y1 and y1 <= 1.05 and 0.0 <= x0 < x1 and x1 <= 1.05):
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
    min_box_width: float = _MIN_BOX_WIDTH,
    min_box_height: float = _MIN_BOX_HEIGHT,
    min_box_area: float = _MIN_BOX_AREA,
) -> tuple[float, float, float, float, float] | None:
    nms = nms_output_to_tensor(nms_output)
    if nms is None:
        logger.warning(
            "parse: nms_output_to_tensor returned None — shape=%s size=%d",
            np.asarray(nms_output).shape,
            np.asarray(nms_output).size,
        )
        return None

    best_score = confidence
    best: tuple[float, float, float, float, float] | None = None
    rejection_reasons: dict[str, int] = {
        "score": 0, "bounds": 0, "width": 0, "height": 0, "area": 0
    }

    for i in range(_MAX_DET):
        score, y0, x0, y1, x1 = (float(v) for v in nms[person_class, i, :])
        if score < 0.01:
            continue  # empty detection slot
        # Hailo NMS may return coordinates in non-canonical order — normalize before bounds check
        if x0 > x1:
            logger.debug("parse: coord swap x (score=%.2f) x0=%.3f x1=%.3f", score, x0, x1)
            x0, x1 = x1, x0
        if y0 > y1:
            logger.debug("parse: coord swap y (score=%.2f) y0=%.3f y1=%.3f", score, y0, y1)
            y0, y1 = y1, y0
        w = x1 - x0
        h = y1 - y0
        if score < confidence:
            rejection_reasons["score"] += 1
        elif not (0.0 <= y0 < y1 and y1 <= 1.05 and 0.0 <= x0 < x1 and x1 <= 1.05):
            rejection_reasons["bounds"] += 1
            logger.info(
                "parse: bounds fail — y0=%.4f x0=%.4f y1=%.4f x1=%.4f (score=%.2f)",
                y0, x0, y1, x1, score,
            )
        elif w < min_box_width:
            rejection_reasons["width"] += 1
            logger.debug(
                "parse: box rejected width=%.3f < %.3f (score=%.2f)", w, min_box_width, score
            )
        elif h < min_box_height:
            rejection_reasons["height"] += 1
            logger.debug(
                "parse: box rejected height=%.3f < %.3f (score=%.2f)", h, min_box_height, score
            )
        elif w * h < min_box_area:
            rejection_reasons["area"] += 1
        else:
            if score > best_score:
                best_score = score
                best = (y0, x0, y1, x1, score)

    if best is None and any(v > 0 for v in rejection_reasons.values()):
        logger.info("parse: all boxes rejected — reasons: %s", rejection_reasons)

    return best
