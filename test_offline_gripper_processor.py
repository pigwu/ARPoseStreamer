import math
import tempfile
import unittest
from pathlib import Path

import numpy as np

from aruco_gripper_tracker import CameraIntrinsics
from offline_gripper_processor import (
    _offline_stabilize,
    load_ultrawide_intrinsics,
    save_ultrawide_intrinsics,
)


class OfflineGripperProcessorTests(unittest.TestCase):
    def test_centered_median_and_short_gap_interpolation(self) -> None:
        values = np.asarray([10.0, 10.2, math.nan, 10.4, 10.6], dtype=np.float64)

        stable, interpolated = _offline_stabilize(values)

        np.testing.assert_allclose(stable, [10.1, 10.2, 10.3, 10.4, 10.5])
        np.testing.assert_array_equal(interpolated, [False, False, True, False, False])

    def test_live_intrinsics_round_trip(self) -> None:
        intrinsics = CameraIntrinsics(285.0, 286.0, 320.0, 240.0, 640, 480)
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "ultrawide_intrinsics.json"

            self.assertTrue(save_ultrawide_intrinsics(path, intrinsics))
            loaded = load_ultrawide_intrinsics(path)

        self.assertEqual(loaded, intrinsics)


if __name__ == "__main__":
    unittest.main()
