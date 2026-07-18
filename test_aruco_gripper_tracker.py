import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

try:
    import cv2
except Exception:
    cv2 = None

from aruco_gripper_tracker import (
    ArucoEstimator,
    CameraIntrinsics,
    FrameAssembly,
    GripperDistanceProcessor,
    MarkerEstimate,
    TrackerConfig,
    calculate_distance_calibration,
    decode_video_fragment,
    summarize_cyclic_calibration,
)
from aruco_robot_pose_receiver import extract_safe_gripper_distance


def make_config(**overrides) -> TrackerConfig:
    values = {
        "dictionary_name": "DICT_4X4_50",
        "marker_size_m": 0.016,
        "marker_ids": (0, 1),
        "distortion_coefficients": np.zeros((5, 1)),
        "fallback_intrinsics": None,
        "max_reprojection_error_px": 2.5,
        "min_marker_perimeter_px": 80.0,
        "tracking_enabled": True,
        "output_host": "127.0.0.1",
        "output_port": 5570,
        "distance_scale": 1.0,
        "distance_offset_m": 0.0,
        "distance_smoothing_alpha": 1.0,
        "distance_measurement_mode": "camera_x",
        "nominal_marker_depth_m": 0.072,
        "marker_depth_tolerance_m": 0.008,
        "calibration_min_raw_m": None,
        "calibration_min_gap_m": 0.0,
        "calibration_max_raw_m": None,
        "calibration_max_gap_m": 0.0,
        "calibration_min_cycles": 5,
    }
    values.update(overrides)
    return TrackerConfig(**values)


class ProtocolTests(unittest.TestCase):
    def test_apv2_fragment_carries_camera_intrinsics(self) -> None:
        packet = struct.pack(
            "<4sBBHIdHHHHffffHH",
            b"APV2",
            2,
            1,
            0,
            42,
            1_700_000_000.25,
            0,
            1,
            0,
            1,
            900.0,
            901.0,
            640.0,
            360.0,
            1280,
            720,
        ) + b"nalu"

        fragment = decode_video_fragment(packet)

        self.assertEqual(fragment.frame_id, 42)
        self.assertTrue(fragment.is_keyframe)
        self.assertEqual(fragment.payload, b"nalu")
        self.assertEqual(
            fragment.camera_intrinsics,
            CameraIntrinsics(900.0, 901.0, 640.0, 360.0, 1280, 720),
        )

    def test_apv1_remains_supported_without_intrinsics(self) -> None:
        packet = struct.pack(
            "<4sBBHIdHHHH",
            b"APV1",
            1,
            0,
            0,
            7,
            123.5,
            0,
            1,
            0,
            1,
        ) + b"legacy"

        fragment = decode_video_fragment(packet)

        self.assertEqual(fragment.frame_id, 7)
        self.assertIsNone(fragment.camera_intrinsics)
        self.assertEqual(fragment.payload, b"legacy")

    def test_frame_reassembles_fragments_in_order(self) -> None:
        first = decode_video_fragment(
            struct.pack("<4sBBHIdHHHH", b"APV1", 1, 0, 0, 1, 2.0, 0, 1, 1, 2) + b"B"
        )
        second = decode_video_fragment(
            struct.pack("<4sBBHIdHHHH", b"APV1", 1, 0, 0, 1, 2.0, 0, 1, 0, 2) + b"A"
        )
        frame = FrameAssembly(1, 2.0, 1, False, 0.0, 0.0, None)

        frame.add(first)
        frame.add(second)

        self.assertTrue(frame.complete())
        self.assertEqual(frame.annexb(), b"\x00\x00\x00\x01AB")


class CalibrationTests(unittest.TestCase):
    def test_intrinsics_scale_with_decoded_resolution(self) -> None:
        intrinsics = CameraIntrinsics(1000.0, 900.0, 640.0, 360.0, 1280, 720)

        matrix = intrinsics.matrix_for(640, 360)

        np.testing.assert_allclose(
            matrix,
            [[500.0, 0.0, 320.0], [0.0, 450.0, 180.0], [0.0, 0.0, 1.0]],
        )

    def test_two_point_calibration_returns_scale_and_offset(self) -> None:
        scale, offset_m = calculate_distance_calibration(0.050, 0.002, 0.090, 0.082)

        self.assertAlmostEqual(scale, 2.0)
        self.assertAlmostEqual(offset_m, -0.098)
        self.assertAlmostEqual(scale * 0.050 + offset_m, 0.002)
        self.assertAlmostEqual(scale * 0.090 + offset_m, 0.082)

    def test_two_point_calibration_rejects_reversed_points(self) -> None:
        with self.assertRaisesRegex(ValueError, "原始标记距离"):
            calculate_distance_calibration(0.090, 0.0, 0.050, 0.080)

    def test_five_cycle_collection_returns_robust_endpoints(self) -> None:
        opening = np.linspace(0.040, 0.120, 31)
        closing = np.linspace(0.120, 0.040, 31)[1:]
        samples = np.tile(np.concatenate([opening, closing]), 5)
        samples += np.sin(np.arange(samples.size)) * 0.0001

        summary = summarize_cyclic_calibration(samples)

        self.assertGreaterEqual(summary.cycle_count, 5)
        self.assertAlmostEqual(summary.minimum_raw_m, 0.040, delta=0.002)
        self.assertAlmostEqual(summary.maximum_raw_m, 0.120, delta=0.002)

    def test_cycle_collection_rejects_too_little_motion(self) -> None:
        with self.assertRaisesRegex(ValueError, "范围不足"):
            summarize_cyclic_calibration(np.linspace(0.0500, 0.0505, 30))

    def test_config_save_and_load_preserves_calibration_points(self) -> None:
        config = make_config(
            fallback_intrinsics=CameraIntrinsics(900.0, 901.0, 640.0, 360.0, 1280, 720),
            output_host="192.168.1.20",
            output_port=6000,
            distance_scale=2.0,
            distance_offset_m=-0.098,
            distance_smoothing_alpha=0.35,
            nominal_marker_depth_m=0.074,
            marker_depth_tolerance_m=0.009,
            calibration_min_raw_m=0.050,
            calibration_min_gap_m=0.002,
            calibration_max_raw_m=0.090,
            calibration_max_gap_m=0.082,
            calibration_min_cycles=6,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aruco.json"
            config.save(path)
            loaded = TrackerConfig.load(path)

        self.assertEqual(loaded.to_json_dict(), config.to_json_dict())

    @unittest.skipUnless(cv2 is not None and hasattr(cv2, "aruco"), "OpenCV ArUco is unavailable")
    def test_synthetic_16mm_marker_recovers_metric_depth(self) -> None:
        config = make_config()
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        marker = cv2.aruco.generateImageMarker(dictionary, 0, 200)
        image = np.full((720, 1280, 3), 255, dtype=np.uint8)
        image[260:460, 540:740] = cv2.cvtColor(marker, cv2.COLOR_GRAY2BGR)

        estimates = ArucoEstimator(config).detect(
            image,
            CameraIntrinsics(900.0, 900.0, 640.0, 360.0, 1280, 720),
        )

        self.assertEqual([estimate.marker_id for estimate in estimates], [0])
        self.assertAlmostEqual(float(estimates[0].transform_camera_marker[2, 3]), 0.072, delta=0.001)

    @unittest.skipUnless(cv2 is not None and hasattr(cv2, "aruco"), "OpenCV ArUco is unavailable")
    def test_processor_outputs_calibrated_two_marker_distance(self) -> None:
        config = make_config(
            max_reprojection_error_px=10.0,
            distance_scale=1.5,
            distance_offset_m=-0.010,
            calibration_min_raw_m=0.030,
            calibration_min_gap_m=0.035,
            calibration_max_raw_m=0.070,
            calibration_max_gap_m=0.095,
        )
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        marker0 = cv2.aruco.generateImageMarker(dictionary, 0, 200)
        marker1 = cv2.aruco.generateImageMarker(dictionary, 1, 200)
        image = np.full((720, 1280, 3), 255, dtype=np.uint8)
        image[260:460, 300:500] = cv2.cvtColor(marker0, cv2.COLOR_GRAY2BGR)
        image[260:460, 780:980] = cv2.cvtColor(marker1, cv2.COLOR_GRAY2BGR)

        result = GripperDistanceProcessor(config).process(
            image,
            frame_id=12,
            capture_timestamp=10.0,
            camera_intrinsics=CameraIntrinsics(900.0, 900.0, 640.0, 360.0, 1280, 720),
        )

        self.assertEqual(result["status"], "tracking_gripper_distance")
        self.assertEqual(set(result["detected_ids"]), {0, 1})
        raw_m = result["gripper_distance"]["raw_marker_x_distance_m"]
        calibrated_m = result["gripper_distance"]["calibrated_m"]
        self.assertAlmostEqual(raw_m, 0.03754, delta=0.001)
        self.assertAlmostEqual(calibrated_m, 1.5 * raw_m - 0.010, places=6)
        self.assertEqual(result["gripper_distance"]["measurement_mode"], "camera_x")
        self.assertGreaterEqual(
            result["gripper_distance"]["marker_center_distance_3d_m"],
            raw_m,
        )
        self.assertTrue(result["gripper_distance"]["calibration_complete"])
        self.assertNotIn("tool", result)

    @unittest.skipUnless(cv2 is not None and hasattr(cv2, "aruco"), "OpenCV ArUco is unavailable")
    def test_processor_rejects_marker_outside_nominal_depth(self) -> None:
        config = make_config()
        processor = GripperDistanceProcessor(config)
        first = np.eye(4)
        first[:3, 3] = [-0.02, 0.0, 0.072]
        second = np.eye(4)
        second[:3, 3] = [0.02, 0.0, 0.100]

        class FakeEstimator:
            def detect(self, _image, _intrinsics):
                return [
                    MarkerEstimate(0, first, 0.1, 200.0),
                    MarkerEstimate(1, second, 0.1, 200.0),
                ]

        processor.estimator = FakeEstimator()
        result = processor.process(
            np.zeros((100, 100, 3), dtype=np.uint8),
            frame_id=1,
            capture_timestamp=1.0,
            camera_intrinsics=CameraIntrinsics(100.0, 100.0, 50.0, 50.0, 100, 100),
        )

        self.assertEqual(result["status"], "marker_depth_out_of_range")
        self.assertEqual(result["measurement"]["invalid_depth_ids"], [1])
        self.assertIsNone(result["gripper_distance"])


class DistanceGateTests(unittest.TestCase):
    @staticmethod
    def message(distance_mm: float, status: str = "tracking_gripper_distance") -> dict:
        return {
            "protocol": "AGP1",
            "status": status,
            "gripper_distance": {
                "calibrated_mm": distance_mm,
                "filtered_mm": distance_mm,
                "calibration_complete": True,
            },
        }

    def test_valid_distance_passes(self) -> None:
        result = extract_safe_gripper_distance(self.message(45.0), None, 0.0, 80.0, 10.0)

        self.assertEqual(result, 45.0)

    def test_missing_marker_status_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not valid"):
            extract_safe_gripper_distance(
                self.message(45.0, "insufficient_markers_for_distance"),
                None,
                0.0,
                80.0,
                10.0,
            )

    def test_incomplete_calibration_is_rejected(self) -> None:
        message = self.message(45.0)
        message["gripper_distance"]["calibration_complete"] = False
        with self.assertRaisesRegex(ValueError, "calibration is incomplete"):
            extract_safe_gripper_distance(message, None, 0.0, 80.0, 10.0)

    def test_out_of_range_and_large_jump_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside configured limits"):
            extract_safe_gripper_distance(self.message(90.0), None, 0.0, 80.0, 10.0)
        with self.assertRaisesRegex(ValueError, "jump"):
            extract_safe_gripper_distance(self.message(45.0), 20.0, 0.0, 80.0, 10.0)


if __name__ == "__main__":
    unittest.main()
