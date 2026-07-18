from __future__ import annotations

import argparse
import csv
import json
import math
import signal
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from select import select
from typing import Any

import numpy as np

try:
    import av
except Exception as exc:  # pragma: no cover - runtime dependency guard
    av = None
    AV_IMPORT_ERROR = exc
else:
    AV_IMPORT_ERROR = None

try:
    import cv2
except Exception as exc:  # pragma: no cover - runtime dependency guard
    cv2 = None
    CV2_IMPORT_ERROR = exc
else:
    CV2_IMPORT_ERROR = None


VIDEO_HEADER_V1 = struct.Struct("<4sBBHIdHHHH")
VIDEO_HEADER_V2 = struct.Struct("<4sBBHIdHHHHffffHH")
FRAME_STALE_SECONDS = 0.20
MAX_INFLIGHT_FRAMES = 8


@dataclass(frozen=True)
class CameraIntrinsics:
    fx: float
    fy: float
    cx: float
    cy: float
    image_width: int
    image_height: int

    def __post_init__(self) -> None:
        values = (self.fx, self.fy, self.cx, self.cy)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Camera intrinsics contain a non-finite value")
        if self.fx <= 0.0 or self.fy <= 0.0 or self.image_width <= 0 or self.image_height <= 0:
            raise ValueError("Camera focal lengths and calibration resolution must be positive")

    def matrix_for(self, width: int, height: int) -> np.ndarray:
        sx = float(width) / float(self.image_width)
        sy = float(height) / float(self.image_height)
        return np.array(
            [
                [self.fx * sx, 0.0, self.cx * sx],
                [0.0, self.fy * sy, self.cy * sy],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )


@dataclass(frozen=True)
class VideoFragment:
    flags: int
    frame_id: int
    capture_timestamp: float
    nalu_index: int
    nalu_count: int
    fragment_index: int
    fragment_count: int
    payload: bytes
    camera_intrinsics: CameraIntrinsics | None

    @property
    def is_keyframe(self) -> bool:
        return bool(self.flags & 0x01)


@dataclass
class NALAssembly:
    total_fragments: int
    fragments: dict[int, bytes] = field(default_factory=dict)

    def complete(self) -> bool:
        return len(self.fragments) == self.total_fragments


@dataclass
class FrameAssembly:
    frame_id: int
    capture_timestamp: float
    nalu_count: int
    is_keyframe: bool
    created_at: float
    last_update_at: float
    camera_intrinsics: CameraIntrinsics | None
    nalus: dict[int, NALAssembly] = field(default_factory=dict)

    def add(self, fragment: VideoFragment) -> None:
        if fragment.nalu_count != self.nalu_count:
            raise ValueError("NAL count changed within a frame")
        if not (0 <= fragment.nalu_index < fragment.nalu_count):
            raise ValueError("NAL index is outside the advertised range")
        if not (0 <= fragment.fragment_index < fragment.fragment_count):
            raise ValueError("Fragment index is outside the advertised range")
        if self.camera_intrinsics is None:
            self.camera_intrinsics = fragment.camera_intrinsics

        nalu = self.nalus.get(fragment.nalu_index)
        if nalu is None:
            nalu = NALAssembly(total_fragments=fragment.fragment_count)
            self.nalus[fragment.nalu_index] = nalu
        elif nalu.total_fragments != fragment.fragment_count:
            raise ValueError("Fragment count changed within a NAL unit")
        nalu.fragments.setdefault(fragment.fragment_index, fragment.payload)

    def complete(self) -> bool:
        return len(self.nalus) == self.nalu_count and all(nalu.complete() for nalu in self.nalus.values())

    def annexb(self) -> bytes:
        chunks: list[bytes] = []
        for nalu_index in range(self.nalu_count):
            nalu = self.nalus.get(nalu_index)
            if nalu is None or not nalu.complete():
                raise ValueError("Cannot decode an incomplete frame")
            payload = b"".join(nalu.fragments[index] for index in range(nalu.total_fragments))
            chunks.append(b"\x00\x00\x00\x01" + payload)
        return b"".join(chunks)


@dataclass(frozen=True)
class MarkerEstimate:
    marker_id: int
    transform_camera_marker: np.ndarray
    reprojection_error_px: float
    perimeter_px: float


@dataclass(frozen=True)
class CyclicCalibrationSummary:
    sample_count: int
    cycle_count: int
    minimum_raw_m: float
    maximum_raw_m: float


@dataclass
class TrackerConfig:
    dictionary_name: str
    marker_size_m: float
    marker_ids: tuple[int, ...]
    distortion_coefficients: np.ndarray
    fallback_intrinsics: CameraIntrinsics | None
    max_reprojection_error_px: float
    min_marker_perimeter_px: float
    tracking_enabled: bool = True
    output_host: str = "127.0.0.1"
    output_port: int = 5570
    distance_scale: float = 1.0
    distance_offset_m: float = 0.0
    distance_smoothing_alpha: float = 1.0
    distance_measurement_mode: str = "camera_x"
    nominal_marker_depth_m: float = 0.072
    marker_depth_tolerance_m: float = 0.008
    calibration_min_raw_m: float | None = None
    calibration_min_gap_m: float = 0.0
    calibration_max_raw_m: float | None = None
    calibration_max_gap_m: float = 0.0
    calibration_min_cycles: int = 5

    @property
    def calibration_complete(self) -> bool:
        return (
            self.calibration_min_raw_m is not None
            and self.calibration_max_raw_m is not None
            and self.calibration_max_raw_m > self.calibration_min_raw_m
            and self.calibration_max_gap_m > self.calibration_min_gap_m
        )

    def __post_init__(self) -> None:
        if self.marker_size_m <= 0.0 or not math.isfinite(self.marker_size_m):
            raise ValueError("marker_size_m must be positive and finite")
        if len(self.marker_ids) != 2 or len(set(self.marker_ids)) != 2:
            raise ValueError("marker_ids must contain exactly two different IDs")
        if not math.isfinite(self.distance_scale) or self.distance_scale <= 0.0:
            raise ValueError("distance_scale must be positive and finite")
        if not math.isfinite(self.distance_offset_m):
            raise ValueError("distance_offset_m must be finite")
        if not 0.0 < self.distance_smoothing_alpha <= 1.0:
            raise ValueError("distance_smoothing_alpha must be in (0, 1]")
        if self.distance_measurement_mode != "camera_x":
            raise ValueError("distance_measurement_mode must be camera_x")
        if not math.isfinite(self.nominal_marker_depth_m) or self.nominal_marker_depth_m <= 0.0:
            raise ValueError("nominal_marker_depth_m must be positive and finite")
        if not math.isfinite(self.marker_depth_tolerance_m) or self.marker_depth_tolerance_m <= 0.0:
            raise ValueError("marker_depth_tolerance_m must be positive and finite")
        if self.calibration_min_cycles < 1:
            raise ValueError("calibration_min_cycles must be positive")
        if not 1 <= self.output_port <= 65535:
            raise ValueError("output_port must be between 1 and 65535")
        for name, value in (
            ("calibration_min_raw_m", self.calibration_min_raw_m),
            ("calibration_min_gap_m", self.calibration_min_gap_m),
            ("calibration_max_raw_m", self.calibration_max_raw_m),
            ("calibration_max_gap_m", self.calibration_max_gap_m),
        ):
            if value is not None and (not math.isfinite(value) or value < 0.0):
                raise ValueError(f"{name} must be non-negative and finite")

    def to_json_dict(self) -> dict[str, Any]:
        fallback = None
        if self.fallback_intrinsics is not None:
            fallback = {
                "fx": self.fallback_intrinsics.fx,
                "fy": self.fallback_intrinsics.fy,
                "cx": self.fallback_intrinsics.cx,
                "cy": self.fallback_intrinsics.cy,
                "image_width": self.fallback_intrinsics.image_width,
                "image_height": self.fallback_intrinsics.image_height,
            }
        return {
            "dictionary": self.dictionary_name,
            "marker_size_m": self.marker_size_m,
            "marker_ids": list(self.marker_ids),
            "distortion_coefficients": self.distortion_coefficients.reshape(-1).tolist(),
            "camera_intrinsics": fallback,
            "max_reprojection_error_px": self.max_reprojection_error_px,
            "min_marker_perimeter_px": self.min_marker_perimeter_px,
            "tracking_enabled": self.tracking_enabled,
            "output_host": self.output_host,
            "output_port": self.output_port,
            "distance_scale": self.distance_scale,
            "distance_offset_m": self.distance_offset_m,
            "distance_smoothing_alpha": self.distance_smoothing_alpha,
            "distance_measurement_mode": self.distance_measurement_mode,
            "nominal_marker_depth_m": self.nominal_marker_depth_m,
            "marker_depth_tolerance_m": self.marker_depth_tolerance_m,
            "distance_calibration": {
                "minimum_cycles": self.calibration_min_cycles,
                "minimum": {
                    "raw_marker_x_distance_m": self.calibration_min_raw_m,
                    "actual_gap_m": self.calibration_min_gap_m,
                },
                "maximum": {
                    "raw_marker_x_distance_m": self.calibration_max_raw_m,
                    "actual_gap_m": self.calibration_max_gap_m,
                },
            },
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_text(
            json.dumps(self.to_json_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    @classmethod
    def load(cls, path: Path) -> "TrackerConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        marker_ids = tuple(int(value) for value in raw.get("marker_ids", [0, 1]))
        marker_size_m = float(raw.get("marker_size_m", 0.016))
        if marker_size_m <= 0:
            raise ValueError("marker_size_m must be positive")
        if len(marker_ids) != 2:
            raise ValueError("marker_ids must contain exactly two IDs for gripper distance")
        fallback_raw = raw.get("camera_intrinsics")
        fallback_intrinsics = None
        if fallback_raw:
            fallback_intrinsics = CameraIntrinsics(
                fx=float(fallback_raw["fx"]),
                fy=float(fallback_raw["fy"]),
                cx=float(fallback_raw["cx"]),
                cy=float(fallback_raw["cy"]),
                image_width=int(fallback_raw["image_width"]),
                image_height=int(fallback_raw["image_height"]),
            )

        calibration = raw.get("distance_calibration") or {}
        minimum = calibration.get("minimum") or {}
        maximum = calibration.get("maximum") or {}

        def optional_float(value: Any) -> float | None:
            return None if value is None else float(value)

        return cls(
            dictionary_name=str(raw.get("dictionary", "DICT_4X4_50")),
            marker_size_m=marker_size_m,
            marker_ids=marker_ids,
            distortion_coefficients=np.asarray(
                raw.get("distortion_coefficients", [0.0, 0.0, 0.0, 0.0, 0.0]),
                dtype=np.float64,
            ).reshape(-1, 1),
            fallback_intrinsics=fallback_intrinsics,
            max_reprojection_error_px=float(raw.get("max_reprojection_error_px", 2.5)),
            min_marker_perimeter_px=float(raw.get("min_marker_perimeter_px", 80.0)),
            tracking_enabled=bool(raw.get("tracking_enabled", True)),
            output_host=str(raw.get("output_host", "127.0.0.1")),
            output_port=int(raw.get("output_port", 5570)),
            distance_scale=float(raw.get("distance_scale", 1.0)),
            distance_offset_m=float(raw.get("distance_offset_m", 0.0)),
            distance_smoothing_alpha=float(raw.get("distance_smoothing_alpha", 1.0)),
            distance_measurement_mode=str(raw.get("distance_measurement_mode", "camera_x")),
            nominal_marker_depth_m=float(raw.get("nominal_marker_depth_m", 0.072)),
            marker_depth_tolerance_m=float(raw.get("marker_depth_tolerance_m", 0.008)),
            calibration_min_raw_m=optional_float(
                minimum.get("raw_marker_x_distance_m", minimum.get("raw_marker_center_m"))
            ),
            calibration_min_gap_m=float(minimum.get("actual_gap_m", 0.0)),
            calibration_max_raw_m=optional_float(
                maximum.get("raw_marker_x_distance_m", maximum.get("raw_marker_center_m"))
            ),
            calibration_max_gap_m=float(maximum.get("actual_gap_m", 0.0)),
            calibration_min_cycles=int(calibration.get("minimum_cycles", 5)),
        )


def calculate_distance_calibration(
    minimum_raw_m: float,
    minimum_gap_m: float,
    maximum_raw_m: float,
    maximum_gap_m: float,
) -> tuple[float, float]:
    """Return scale and offset for actual_gap = scale * raw_distance + offset."""
    values = (minimum_raw_m, minimum_gap_m, maximum_raw_m, maximum_gap_m)
    if not all(math.isfinite(value) and value >= 0.0 for value in values):
        raise ValueError("Calibration distances must be non-negative finite values")
    if maximum_raw_m <= minimum_raw_m:
        raise ValueError("最大点的原始标记距离必须大于最小点")
    if maximum_gap_m <= minimum_gap_m:
        raise ValueError("最大实际开口必须大于最小实际开口")
    scale = (maximum_gap_m - minimum_gap_m) / (maximum_raw_m - minimum_raw_m)
    offset_m = minimum_gap_m - scale * minimum_raw_m
    if not math.isfinite(scale) or not math.isfinite(offset_m) or scale <= 0.0:
        raise ValueError("Two-point calibration produced an invalid mapping")
    return scale, offset_m


def summarize_cyclic_calibration(samples_m: list[float] | np.ndarray) -> CyclicCalibrationSummary:
    """Estimate robust endpoints and completed open-close cycles from raw X widths."""
    samples = np.asarray(samples_m, dtype=np.float64).reshape(-1)
    samples = samples[np.isfinite(samples)]
    if samples.size < 10:
        raise ValueError("至少需要 10 个有效采样帧")
    if np.any(samples < 0.0):
        raise ValueError("标定采样距离不能为负数")

    lower = float(np.percentile(samples, 5.0))
    upper = float(np.percentile(samples, 95.0))
    span = upper - lower
    if not math.isfinite(span) or span < 0.001:
        raise ValueError("采集到的开合范围不足 1 mm")

    low_state_cut = lower + span * 0.25
    high_state_cut = upper - span * 0.25
    extreme_events: list[tuple[int, float]] = []
    for value in samples:
        state = -1 if value <= low_state_cut else 1 if value >= high_state_cut else 0
        if not state:
            continue
        if not extreme_events or extreme_events[-1][0] != state:
            extreme_events.append((state, float(value)))
        else:
            previous = extreme_events[-1][1]
            extreme_events[-1] = (
                state,
                min(previous, float(value)) if state < 0 else max(previous, float(value)),
            )
    low_extrema = [value for state, value in extreme_events if state < 0]
    high_extrema = [value for state, value in extreme_events if state > 0]
    if not low_extrema or not high_extrema:
        raise ValueError("未同时采集到全闭和全开端点")
    minimum_raw_m = float(np.median(low_extrema))
    maximum_raw_m = float(np.median(high_extrema))
    cycle_count = max(0, (len(extreme_events) - 1) // 2)
    return CyclicCalibrationSummary(
        sample_count=int(samples.size),
        cycle_count=cycle_count,
        minimum_raw_m=minimum_raw_m,
        maximum_raw_m=maximum_raw_m,
    )


def decode_video_fragment(packet: bytes) -> VideoFragment:
    if len(packet) < 6:
        raise ValueError("Video packet is shorter than the protocol prefix")
    magic = packet[:4]
    version = packet[4]
    intrinsics = None

    if magic == b"APV1" and version == 1:
        header = VIDEO_HEADER_V1
        if len(packet) < header.size:
            raise ValueError("APV1 packet is shorter than its header")
        values = header.unpack_from(packet)
        _, _, flags, _, frame_id, timestamp, nalu_index, nalu_count, fragment_index, fragment_count = values
    elif magic == b"APV2" and version == 2:
        header = VIDEO_HEADER_V2
        if len(packet) < header.size:
            raise ValueError("APV2 packet is shorter than its header")
        values = header.unpack_from(packet)
        (
            _,
            _,
            flags,
            _,
            frame_id,
            timestamp,
            nalu_index,
            nalu_count,
            fragment_index,
            fragment_count,
            fx,
            fy,
            cx,
            cy,
            image_width,
            image_height,
        ) = values
        intrinsics = CameraIntrinsics(fx, fy, cx, cy, image_width, image_height)
    else:
        raise ValueError(f"Unsupported video protocol {magic!r} version {version}")

    if nalu_count <= 0 or fragment_count <= 0:
        raise ValueError("NAL and fragment counts must be positive")
    return VideoFragment(
        flags=flags,
        frame_id=frame_id,
        capture_timestamp=timestamp,
        nalu_index=nalu_index,
        nalu_count=nalu_count,
        fragment_index=fragment_index,
        fragment_count=fragment_count,
        payload=packet[header.size :],
        camera_intrinsics=intrinsics,
    )


class ArucoEstimator:
    def __init__(self, config: TrackerConfig) -> None:
        if cv2 is None:
            raise RuntimeError(f"OpenCV is unavailable: {CV2_IMPORT_ERROR}")
        if not hasattr(cv2, "aruco"):
            raise RuntimeError("This OpenCV build does not include cv2.aruco")
        if not hasattr(cv2.aruco, config.dictionary_name):
            raise ValueError(f"Unknown ArUco dictionary {config.dictionary_name}")
        dictionary_id = getattr(cv2.aruco, config.dictionary_name)
        dictionary = cv2.aruco.getPredefinedDictionary(dictionary_id)
        if hasattr(cv2.aruco, "DetectorParameters"):
            parameters = cv2.aruco.DetectorParameters()
        else:  # pragma: no cover - OpenCV 4.6 compatibility
            parameters = cv2.aruco.DetectorParameters_create()
        if hasattr(cv2.aruco, "CORNER_REFINE_SUBPIX"):
            parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        self.detector = cv2.aruco.ArucoDetector(dictionary, parameters) if hasattr(cv2.aruco, "ArucoDetector") else None
        self.dictionary = dictionary
        self.parameters = parameters
        self.config = config
        half = config.marker_size_m * 0.5
        self.object_points = np.array(
            [[-half, half, 0.0], [half, half, 0.0], [half, -half, 0.0], [-half, -half, 0.0]],
            dtype=np.float64,
        )

    def detect(self, image_bgr: np.ndarray, intrinsics: CameraIntrinsics) -> list[MarkerEstimate]:
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        if self.detector is not None:
            corners, ids, _ = self.detector.detectMarkers(gray)
        else:  # pragma: no cover - OpenCV 4.6 compatibility
            corners, ids, _ = cv2.aruco.detectMarkers(
                gray,
                self.dictionary,
                parameters=self.parameters,
            )
        if ids is None:
            return []

        camera_matrix = intrinsics.matrix_for(image_bgr.shape[1], image_bgr.shape[0])
        estimates: list[MarkerEstimate] = []
        for marker_corners, marker_id_raw in zip(corners, ids.reshape(-1)):
            marker_id = int(marker_id_raw)
            if marker_id not in self.config.marker_ids:
                continue
            image_points = np.asarray(marker_corners, dtype=np.float64).reshape(4, 2)
            perimeter = float(cv2.arcLength(image_points.astype(np.float32), True))
            if perimeter < self.config.min_marker_perimeter_px:
                continue
            ok, rotation_vector, translation_vector = cv2.solvePnP(
                self.object_points,
                image_points,
                camera_matrix,
                self.config.distortion_coefficients,
                flags=cv2.SOLVEPNP_IPPE_SQUARE,
            )
            if not ok or float(translation_vector[2, 0]) <= 0.0:
                continue
            projected, _ = cv2.projectPoints(
                self.object_points,
                rotation_vector,
                translation_vector,
                camera_matrix,
                self.config.distortion_coefficients,
            )
            reprojection_error = float(
                np.sqrt(np.mean(np.sum((projected.reshape(4, 2) - image_points) ** 2, axis=1)))
            )
            if reprojection_error > self.config.max_reprojection_error_px:
                continue
            rotation, _ = cv2.Rodrigues(rotation_vector)
            transform = np.eye(4, dtype=np.float64)
            transform[:3, :3] = rotation
            transform[:3, 3] = translation_vector.reshape(3)
            estimates.append(MarkerEstimate(marker_id, transform, reprojection_error, perimeter))
        return estimates


class ResultPublisher:
    def __init__(self, host: str, port: int, csv_path: Path | None) -> None:
        self.address = (host, port)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.csv_file = None
        self.csv_writer = None
        if csv_path is not None:
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            self.csv_file = csv_path.open("w", newline="", encoding="utf-8")
            self.csv_writer = csv.writer(self.csv_file)
            self.csv_writer.writerow(
                [
                    "capture_time",
                    "frame_id",
                    "status",
                    "marker_ids",
                    "raw_marker_x_distance_mm",
                    "marker_center_distance_3d_mm",
                    "calibrated_distance_mm",
                    "filtered_distance_mm",
                ]
            )

    def publish(self, result: dict[str, Any]) -> None:
        payload = json.dumps(result, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.socket.sendto(payload, self.address)
        if self.csv_writer is None:
            return
        distance = result.get("gripper_distance") or {}
        self.csv_writer.writerow(
            [
                result["capture_time"],
                result["frame_id"],
                result["status"],
                " ".join(str(value) for value in result.get("detected_ids", [])),
                (
                    float(distance["raw_marker_x_distance_m"]) * 1000.0
                    if distance.get("raw_marker_x_distance_m") is not None
                    else ""
                ),
                (
                    float(distance["marker_center_distance_3d_m"]) * 1000.0
                    if distance.get("marker_center_distance_3d_m") is not None
                    else ""
                ),
                distance.get("calibrated_mm", ""),
                distance.get("filtered_mm", ""),
            ]
        )
        self.csv_file.flush()

    def close(self) -> None:
        self.socket.close()
        if self.csv_file is not None:
            self.csv_file.close()


class GripperDistanceProcessor:
    """Detect two configured markers and produce one calibrated gap per video frame."""

    def __init__(self, config: TrackerConfig) -> None:
        self.config = config
        self.estimator = ArucoEstimator(config)
        self.filtered_distance_m: float | None = None

    def process(
        self,
        image_bgr: np.ndarray,
        frame_id: int,
        capture_timestamp: float,
        camera_intrinsics: CameraIntrinsics | None,
    ) -> dict[str, Any]:
        intrinsics = camera_intrinsics or self.config.fallback_intrinsics
        result: dict[str, Any] = {
            "protocol": "AGP1",
            "capture_time": round(capture_timestamp, 6),
            "frame_id": frame_id,
            "status": "missing_intrinsics",
            "detected_ids": [],
            "markers": {},
            "gripper_distance": None,
            "measurement": {
                "mode": self.config.distance_measurement_mode,
                "nominal_marker_depth_m": self.config.nominal_marker_depth_m,
                "marker_depth_tolerance_m": self.config.marker_depth_tolerance_m,
            },
        }
        if intrinsics is None:
            return result

        estimates = self.estimator.detect(image_bgr, intrinsics)
        result["detected_ids"] = [estimate.marker_id for estimate in estimates]
        if not estimates:
            result["status"] = "no_markers"
            return result

        for estimate in estimates:
            result["markers"][str(estimate.marker_id)] = {
                "center_camera_m": [
                    round(float(value), 7)
                    for value in estimate.transform_camera_marker[:3, 3]
                ],
                "reprojection_error_px": round(estimate.reprojection_error_px, 4),
                "perimeter_px": round(estimate.perimeter_px, 2),
            }

        estimates_by_id = {estimate.marker_id: estimate for estimate in estimates}
        distance_ids = self.config.marker_ids[:2]
        if len(distance_ids) < 2 or not all(marker_id in estimates_by_id for marker_id in distance_ids):
            result["status"] = "insufficient_markers_for_distance"
        else:
            first = estimates_by_id[distance_ids[0]].transform_camera_marker[:3, 3]
            second = estimates_by_id[distance_ids[1]].transform_camera_marker[:3, 3]
            minimum_depth = self.config.nominal_marker_depth_m - self.config.marker_depth_tolerance_m
            maximum_depth = self.config.nominal_marker_depth_m + self.config.marker_depth_tolerance_m
            invalid_depth_ids = [
                marker_id
                for marker_id, position in zip(distance_ids, (first, second))
                if not minimum_depth < float(position[2]) < maximum_depth
            ]
            result["measurement"]["marker_depth_m"] = {
                str(marker_id): round(float(position[2]), 7)
                for marker_id, position in zip(distance_ids, (first, second))
            }
            if invalid_depth_ids:
                result["measurement"]["invalid_depth_ids"] = invalid_depth_ids
                result["status"] = "marker_depth_out_of_range"
                return result

            raw_distance_m = abs(float(second[0] - first[0]))
            center_distance_3d_m = float(np.linalg.norm(second - first))
            calibrated_distance_m = (
                self.config.distance_scale * raw_distance_m + self.config.distance_offset_m
            )
            alpha = float(np.clip(self.config.distance_smoothing_alpha, 0.0, 1.0))
            if self.filtered_distance_m is None or alpha >= 1.0:
                self.filtered_distance_m = calibrated_distance_m
            else:
                self.filtered_distance_m = (
                    alpha * calibrated_distance_m + (1.0 - alpha) * self.filtered_distance_m
                )
            result["gripper_distance"] = {
                "marker_ids": list(distance_ids),
                "measurement_mode": self.config.distance_measurement_mode,
                "raw_marker_x_distance_m": round(raw_distance_m, 8),
                "marker_center_distance_3d_m": round(center_distance_3d_m, 8),
                "calibrated_m": round(calibrated_distance_m, 8),
                "filtered_m": round(self.filtered_distance_m, 8),
                "calibrated_mm": round(calibrated_distance_m * 1000.0, 4),
                "filtered_mm": round(self.filtered_distance_m * 1000.0, 4),
                "scale": self.config.distance_scale,
                "offset_m": self.config.distance_offset_m,
                "calibration_complete": self.config.calibration_complete,
                "calibrated_range_mm": (
                    [
                        round(self.config.calibration_min_gap_m * 1000.0, 4),
                        round(self.config.calibration_max_gap_m * 1000.0, 4),
                    ]
                    if self.config.calibration_complete
                    else None
                ),
            }
            result["status"] = "tracking_gripper_distance"

        return result


class GripperTrackingServer:
    def __init__(self, args: argparse.Namespace, config: TrackerConfig) -> None:
        if av is None:
            raise RuntimeError(f"PyAV is unavailable: {AV_IMPORT_ERROR}")
        self.args = args
        self.config = config
        self.processor = GripperDistanceProcessor(config)
        self.publisher = ResultPublisher(args.output_host, args.output_port, args.csv_log)
        self.decoder = av.CodecContext.create("h264", "r")
        self.frames: dict[int, FrameAssembly] = {}
        self.waiting_for_keyframe = True
        self.latest_decoded_frame_id: int | None = None
        self.running = True
        self.last_console_time = 0.0

    def stop(self, *_args: Any) -> None:
        self.running = False

    def run(self) -> None:
        video_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        video_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        video_socket.bind((self.args.bind, self.args.video_port))
        video_socket.setblocking(False)
        print(
            f"ArUco gripper distance: video {self.args.bind}:{self.args.video_port}, "
            f"output {self.args.output_host}:{self.args.output_port}"
        )
        print("Press Ctrl+C to stop")
        try:
            while self.running:
                readable, _, _ = select([video_socket], [], [], 0.05)
                for current in readable:
                    packet, _address = current.recvfrom(65535)
                    self.handle_video_packet(packet)
                self.prune_frames()
        finally:
            video_socket.close()
            self.publisher.close()

    def handle_video_packet(self, packet: bytes) -> None:
        try:
            fragment = decode_video_fragment(packet)
        except ValueError:
            return
        if self.latest_decoded_frame_id is not None and fragment.frame_id <= self.latest_decoded_frame_id:
            return
        now = time.monotonic()
        frame = self.frames.get(fragment.frame_id)
        if frame is None:
            frame = FrameAssembly(
                frame_id=fragment.frame_id,
                capture_timestamp=fragment.capture_timestamp,
                nalu_count=fragment.nalu_count,
                is_keyframe=fragment.is_keyframe,
                created_at=now,
                last_update_at=now,
                camera_intrinsics=fragment.camera_intrinsics,
            )
            self.frames[fragment.frame_id] = frame
        frame.last_update_at = now
        try:
            frame.add(fragment)
        except ValueError:
            self.frames.pop(fragment.frame_id, None)
            return
        if frame.complete():
            self.frames.pop(frame.frame_id, None)
            self.decode_frame(frame)
        if len(self.frames) > MAX_INFLIGHT_FRAMES:
            oldest = sorted(self.frames.values(), key=lambda item: item.created_at)
            for stale in oldest[:-MAX_INFLIGHT_FRAMES]:
                self.frames.pop(stale.frame_id, None)

    def prune_frames(self) -> None:
        now = time.monotonic()
        for frame_id, frame in list(self.frames.items()):
            if now - frame.last_update_at >= FRAME_STALE_SECONDS:
                self.frames.pop(frame_id, None)

    def decode_frame(self, frame: FrameAssembly) -> None:
        if self.waiting_for_keyframe and not frame.is_keyframe:
            return
        try:
            if frame.is_keyframe:
                self.decoder = av.CodecContext.create("h264", "r")
                self.waiting_for_keyframe = False
            packet = av.Packet(frame.annexb())
            decoded_frames = self.decoder.decode(packet)
            if not decoded_frames:
                for parsed in self.decoder.parse(frame.annexb()):
                    decoded_frames.extend(self.decoder.decode(parsed))
            if not decoded_frames:
                if frame.is_keyframe:
                    self.waiting_for_keyframe = True
                return
            for decoded in decoded_frames:
                self.process_image(decoded.to_ndarray(format="bgr24"), frame)
            self.latest_decoded_frame_id = frame.frame_id
        except Exception as exc:
            self.waiting_for_keyframe = True
            self.decoder = av.CodecContext.create("h264", "r")
            print(f"Decode error on frame {frame.frame_id}: {exc}", file=sys.stderr)

    def process_image(self, image_bgr: np.ndarray, frame: FrameAssembly) -> None:
        result = self.processor.process(
            image_bgr=image_bgr,
            frame_id=frame.frame_id,
            capture_timestamp=frame.capture_timestamp,
            camera_intrinsics=frame.camera_intrinsics,
        )
        self.emit(result)

    def emit(self, result: dict[str, Any]) -> None:
        self.publisher.publish(result)
        now = time.monotonic()
        if now - self.last_console_time >= self.args.print_interval:
            self.last_console_time = now
            distance = result.get("gripper_distance") or {}
            distance_text = (
                f" distance={distance['filtered_mm']:.3f}mm"
                if distance.get("filtered_mm") is not None
                else ""
            )
            print(
                f"frame={result['frame_id']} status={result['status']} "
                f"ids={result['detected_ids']}{distance_text}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Track the printed UMI gripper ArUco ID 0/1 pair from ARPoseStreamer video."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "config" / "umi_gripper_aruco.json",
        help="JSON calibration/configuration file.",
    )
    parser.add_argument("--bind", default="0.0.0.0", help="Local address for the video UDP socket.")
    parser.add_argument("--video-port", type=int, default=5560, help="ARPoseStreamer APV1/APV2 video UDP port.")
    parser.add_argument("--output-host", default="127.0.0.1", help="Gripper-distance destination address.")
    parser.add_argument("--output-port", type=int, default=5570, help="AGP1 JSON output UDP port.")
    parser.add_argument("--csv-log", type=Path, default=None, help="Optional tracking CSV path.")
    parser.add_argument("--print-interval", type=float, default=0.25, help="Console update interval in seconds.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = TrackerConfig.load(args.config)
        server = GripperTrackingServer(args, config)
    except Exception as exc:
        print(f"Configuration/startup error: {exc}", file=sys.stderr)
        return 2
    signal.signal(signal.SIGINT, server.stop)
    signal.signal(signal.SIGTERM, server.stop)
    try:
        server.run()
    except OSError as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
