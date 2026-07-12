"""Capture a live NMS output tensor from Hailo and save it as a test fixture.

Run on the Pi (with rover2-api running) while a person is in view:

    python3 /opt/rover2/capture_nms_fixture.py [--name live_sample]

The fixture is saved to /opt/rover2/tests/fixtures/nms_<name>.npy and can be
copied back to the dev machine at tests/fixtures/nms_live_sample.npy for
offline regression tests.

This script is STANDALONE — it does NOT import or modify any running service.
It re-uses the body_tracker path to obtain one NMS tensor and exits.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

_FIXTURE_DIR = Path(__file__).parent.parent / "tests" / "fixtures"
_EXPECTED_DENSE_SIZE = 80 * 100 * 5   # 40000
_EXPECTED_PADDED_SIZE = 80 * 501       # 40080


def save_fixture(nms_output: np.ndarray, name: str = "live_sample", out_dir: Path | None = None) -> Path:
    """Validate and persist one NMS tensor.

    Args:
        nms_output: Raw Hailo NMS output (1-D or any shape; will be ravelled).
        name:       Output file stem — saved as {out_dir}/nms_{name}.npy.
        out_dir:    Destination directory; defaults to tests/fixtures/ (repo-relative).

    Returns:
        Path to the saved .npy file.

    Raises:
        ValueError: If the array size does not match either known NMS format.
    """
    arr = np.asarray(nms_output, dtype=np.float32).ravel()
    if arr.size not in (_EXPECTED_DENSE_SIZE, _EXPECTED_PADDED_SIZE):
        raise ValueError(
            f"Unexpected NMS output size {arr.size}; "
            f"expected {_EXPECTED_DENSE_SIZE} (dense) or {_EXPECTED_PADDED_SIZE} (padded-501)."
        )
    target_dir = out_dir if out_dir is not None else _FIXTURE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"nms_{name}.npy"
    np.save(path, arr)
    print(f"[capture_nms_fixture] saved {arr.size} floats → {path}")
    return path


def _capture_one(model_path: str, hef_path: str) -> np.ndarray:
    """Fetch one NMS tensor from the live Hailo inference session.

    Uses hailo_platform directly (not through body_tracker) so it does not
    disturb the running service.  Requires FOLLOW to be OFF.
    """
    try:
        from hailo_platform import VDevice, HEF  # type: ignore[import]
        from hailo_platform.pyhailort import FormatType  # type: ignore[import]
    except ImportError as exc:
        sys.exit(f"hailo_platform not available: {exc}")

    params = VDevice.create_params()
    with VDevice(params) as vd:
        hef = HEF(hef_path)
        configure_params = {
            g.name: vd.create_configure_params_by_data_layout(hef)
            for g in hef.get_network_groups()
        }
        with vd.configure(hef, configure_params) as configured_net_group:
            network_groups = configured_net_group
            ng = network_groups[0]
            with ng.activate({}) as activated_ng:
                input_vstreams_params = ng.make_input_vstream_params(
                    True, FormatType.FLOAT32, None,
                )
                output_vstreams_params = ng.make_output_vstream_params(
                    False, FormatType.FLOAT32, None,
                )
                with ng.get_input_vstreams(input_vstreams_params) as (input_vstream,):
                    with ng.get_output_vstreams(output_vstreams_params) as (output_vstream,):
                        dummy = np.zeros(input_vstream.info.shape, dtype=np.float32)
                        input_vstream.write(dummy)
                        raw = output_vstream.read()
                        return np.asarray(raw, dtype=np.float32).ravel()


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture live Hailo NMS fixture")
    parser.add_argument("--name", default="live_sample", help="Fixture file stem")
    parser.add_argument(
        "--hef",
        default="/opt/rover2/yolov8s_h8l.hef",
        help="Path to YOLOv8 HEF file",
    )
    args = parser.parse_args()

    print(f"[capture_nms_fixture] capturing from Hailo (hef={args.hef}) …")
    try:
        raw = _capture_one(model_path="", hef_path=args.hef)
        save_fixture(raw, name=args.name)
    except SystemExit:
        raise
    except Exception as exc:
        sys.exit(f"Capture failed: {exc}")


if __name__ == "__main__":
    main()
