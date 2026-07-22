from __future__ import annotations

import csv
import hashlib
import json
import math
import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import av
import cv2
import numpy as np

from aruco_gripper_tracker import CameraIntrinsics, GripperDistanceProcessor, TrackerConfig
from experiment_data import ExperimentDataset


RESULT_NAME = "aruco_gripper.csv"
STATE_NAME = "aruco_gripper_state.json"
INTRINSICS_NAME = "ultrawide_intrinsics.json"
CSV_FIELDS = [
    "frame_index",
    "video_time",
    "experiment_time",
    "status",
    "detected_ids",
    "valid",
    "interpolated",
    "raw_marker_x_distance_m",
    "calibrated_mm",
    "offline_smoothed_mm",
    "marker_0_depth_m",
    "marker_1_depth_m",
    "max_reprojection_error_px",
]


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json_atomic(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def intrinsics_to_dict(intrinsics: object) -> dict:
    return {
        "fx": float(getattr(intrinsics, "fx")),
        "fy": float(getattr(intrinsics, "fy")),
        "cx": float(getattr(intrinsics, "cx")),
        "cy": float(getattr(intrinsics, "cy")),
        "image_width": int(getattr(intrinsics, "image_width")),
        "image_height": int(getattr(intrinsics, "image_height")),
    }


def save_ultrawide_intrinsics(path: Path, intrinsics: object) -> bool:
    value = intrinsics_to_dict(intrinsics)
    value.update(
        {
            "source": "APV2 live stream",
            "updated_at": datetime.now().isoformat(timespec="milliseconds"),
        }
    )
    existing = _read_json(path)
    if all(existing.get(key) == value[key] for key in intrinsics_to_dict(intrinsics)):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(path, value)
    return True


def load_ultrawide_intrinsics(path: Path) -> CameraIntrinsics | None:
    raw = _read_json(path)
    try:
        return CameraIntrinsics(
            fx=float(raw["fx"]),
            fy=float(raw["fy"]),
            cx=float(raw["cx"]),
            cy=float(raw["cy"]),
            image_width=int(raw["image_width"]),
            image_height=int(raw["image_height"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def estimate_intrinsics_from_video(
    video_path: Path,
    config: TrackerConfig,
    maximum_frames: int = 120,
) -> CameraIntrinsics:
    dictionary_id = getattr(cv2.aruco, config.dictionary_name, None)
    if dictionary_id is None:
        raise ValueError(f"Unknown ArUco dictionary: {config.dictionary_name}")
    dictionary = cv2.aruco.getPredefinedDictionary(dictionary_id)
    detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
    configured_ids = set(config.marker_ids)
    focal_samples: list[float] = []
    width = height = 0

    with av.open(str(video_path)) as container:
        for frame_index, frame in enumerate(container.decode(video=0)):
            if frame_index >= maximum_frames:
                break
            image = frame.to_ndarray(format="gray")
            height, width = image.shape[:2]
            corners, ids, _rejected = detector.detectMarkers(image)
            if ids is None:
                continue
            for marker_corners, marker_id in zip(corners, ids.reshape(-1)):
                if int(marker_id) not in configured_ids:
                    continue
                points = np.asarray(marker_corners, dtype=np.float64).reshape(4, 2)
                side_lengths = [
                    float(np.linalg.norm(points[(index + 1) % 4] - points[index]))
                    for index in range(4)
                ]
                representative_side = float(np.mean(side_lengths))
                focal = representative_side * config.nominal_marker_depth_m / config.marker_size_m
                if math.isfinite(focal) and focal > 0.0:
                    focal_samples.append(focal)

    if not focal_samples or width <= 0 or height <= 0:
        raise ValueError("Could not estimate ultra-wide intrinsics because no configured markers were detected")
    focal = float(np.median(np.asarray(focal_samples, dtype=np.float64)))
    if not 0.1 * width <= focal <= 5.0 * width:
        raise ValueError(f"Estimated focal length is implausible: {focal:.3f} px")
    return CameraIntrinsics(
        fx=focal,
        fy=focal,
        cx=width * 0.5,
        cy=height * 0.5,
        image_width=width,
        image_height=height,
    )


def _offline_stabilize(values: np.ndarray, half_window: int = 2, maximum_gap: int = 2) -> tuple[np.ndarray, np.ndarray]:
    smoothed = np.full(values.shape, np.nan, dtype=np.float64)
    for index, value in enumerate(values):
        if not math.isfinite(float(value)):
            continue
        start = max(0, index - half_window)
        stop = min(values.size, index + half_window + 1)
        local = values[start:stop]
        local = local[np.isfinite(local)]
        if local.size:
            smoothed[index] = float(np.median(local))

    interpolated = np.zeros(values.shape, dtype=bool)
    index = 0
    while index < smoothed.size:
        if math.isfinite(float(smoothed[index])):
            index += 1
            continue
        start = index
        while index < smoothed.size and not math.isfinite(float(smoothed[index])):
            index += 1
        gap = index - start
        if gap <= maximum_gap and start > 0 and index < smoothed.size:
            left = smoothed[start - 1]
            right = smoothed[index]
            for offset in range(gap):
                fraction = (offset + 1) / (gap + 1)
                smoothed[start + offset] = left + (right - left) * fraction
                interpolated[start + offset] = True
    return smoothed, interpolated


def _register_result(directory: Path) -> None:
    state_path = directory / "upload_state.json"
    state = _read_json(state_path)
    components = state.get("components") if isinstance(state.get("components"), dict) else {}
    components["aruco_gripper"] = RESULT_NAME
    state["components"] = components
    _write_json_atomic(state_path, state)


def process_experiment(
    directory: Path,
    config_path: Path,
    intrinsics_path: Path,
) -> dict:
    directory = directory.expanduser().resolve()
    dataset = ExperimentDataset.load(directory)
    video_path = dataset.ultrawide_video_path
    if video_path is None or not video_path.is_file():
        raise ValueError("Experiment does not contain ultrawide_video.mp4")
    config = TrackerConfig.load(config_path)
    if not config.calibration_complete:
        raise ValueError("Two-point gripper calibration is incomplete")

    intrinsics = load_ultrawide_intrinsics(intrinsics_path)
    intrinsics_source = "APV2 live stream"
    if intrinsics is None:
        intrinsics = estimate_intrinsics_from_video(video_path, config)
        intrinsics_source = "estimated from marker size and nominal depth"

    state_path = directory / STATE_NAME
    output_path = directory / RESULT_NAME
    video_stat = video_path.stat()
    signature_value = {
        "video_size": video_stat.st_size,
        "video_mtime_ns": video_stat.st_mtime_ns,
        "config": config.to_json_dict(),
        "intrinsics": intrinsics_to_dict(intrinsics),
    }
    signature = hashlib.sha256(
        json.dumps(signature_value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    running_state = {
        "status": "running",
        "source_video": video_path.name,
        "output": RESULT_NAME,
        "signature": signature,
        "intrinsics_source": intrinsics_source,
        "intrinsics": intrinsics_to_dict(intrinsics),
        "updated_at": datetime.now().isoformat(timespec="milliseconds"),
    }
    _write_json_atomic(state_path, running_state)

    processor = GripperDistanceProcessor(config)
    rows: list[dict[str, object]] = []
    calibrated_values: list[float] = []
    with av.open(str(video_path)) as container:
        stream = container.streams.video[0]
        fallback_rate = float(stream.average_rate or 10.0)
        for frame_index, frame in enumerate(container.decode(stream)):
            video_time = float(frame.time) if frame.time is not None else frame_index / fallback_rate
            image_bgr = frame.to_ndarray(format="bgr24")
            result = processor.process(
                image_bgr=image_bgr,
                frame_id=frame_index,
                capture_timestamp=video_time,
                camera_intrinsics=intrinsics,
            )
            distance = result.get("gripper_distance") or {}
            measurement = result.get("measurement") or {}
            depths = measurement.get("marker_depth_m") or {}
            errors = [
                marker.get("reprojection_error_px")
                for marker in (result.get("markers") or {}).values()
                if isinstance(marker, dict) and isinstance(marker.get("reprojection_error_px"), (int, float))
            ]
            calibrated_mm = distance.get("calibrated_mm")
            calibrated_values.append(float(calibrated_mm) if isinstance(calibrated_mm, (int, float)) else math.nan)
            rows.append(
                {
                    "frame_index": frame_index,
                    "video_time": f"{video_time:.9f}",
                    "experiment_time": f"{dataset.ultrawide_video_start_offset_seconds + video_time:.9f}",
                    "status": result.get("status", "--"),
                    "detected_ids": ";".join(str(value) for value in (result.get("detected_ids") or [])),
                    "valid": int(isinstance(calibrated_mm, (int, float))),
                    "interpolated": 0,
                    "raw_marker_x_distance_m": distance.get("raw_marker_x_distance_m", ""),
                    "calibrated_mm": calibrated_mm if isinstance(calibrated_mm, (int, float)) else "",
                    "offline_smoothed_mm": "",
                    "marker_0_depth_m": depths.get("0", ""),
                    "marker_1_depth_m": depths.get("1", ""),
                    "max_reprojection_error_px": max(errors) if errors else "",
                }
            )

    values = np.asarray(calibrated_values, dtype=np.float64)
    smoothed, interpolated = _offline_stabilize(values)
    for index, row in enumerate(rows):
        if math.isfinite(float(smoothed[index])):
            row["offline_smoothed_mm"] = f"{smoothed[index]:.6f}"
        row["interpolated"] = int(interpolated[index])

    temporary_output = output_path.with_suffix(output_path.suffix + ".part")
    with temporary_output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary_output.replace(output_path)
    _register_result(directory)

    valid_frames = int(np.isfinite(values).sum())
    completed_state = {
        **running_state,
        "status": "complete",
        "frame_count": len(rows),
        "valid_frame_count": valid_frames,
        "interpolated_frame_count": int(interpolated.sum()),
        "detection_rate": valid_frames / len(rows) if rows else 0.0,
        "updated_at": datetime.now().isoformat(timespec="milliseconds"),
    }
    _write_json_atomic(state_path, completed_state)
    return completed_state


@dataclass(frozen=True)
class _Job:
    directory: Path
    config_path: Path
    intrinsics_path: Path
    callback: Callable[[dict], None] | None


class OfflineGripperProcessor:
    def __init__(self) -> None:
        self._queue: queue.Queue[_Job] = queue.Queue()
        self._lock = threading.Lock()
        self._pending: set[Path] = set()
        self._worker: threading.Thread | None = None

    def schedule(
        self,
        directory: Path,
        config_path: Path,
        intrinsics_path: Path,
        callback: Callable[[dict], None] | None = None,
        *,
        force: bool = False,
    ) -> bool:
        directory = directory.expanduser().resolve()
        state = _read_json(directory / STATE_NAME)
        if not force and state.get("status") == "complete" and (directory / RESULT_NAME).is_file():
            return False
        with self._lock:
            if directory in self._pending:
                return False
            self._pending.add(directory)
            self._queue.put(_Job(directory, config_path.resolve(), intrinsics_path.resolve(), callback))
            self._emit(callback, directory, "queued")
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, daemon=True, name="offline-gripper")
                self._worker.start()
        return True

    def backfill(
        self,
        root: Path,
        config_path: Path,
        intrinsics_path: Path,
        callback: Callable[[dict], None] | None = None,
    ) -> int:
        root = root.expanduser().resolve()
        if not root.is_dir():
            return 0
        scheduled = 0
        for directory in sorted(path for path in root.iterdir() if path.is_dir()):
            try:
                dataset = ExperimentDataset.load(directory)
            except Exception:
                continue
            if dataset.ultrawide_video_path is None:
                continue
            if self.schedule(directory, config_path, intrinsics_path, callback):
                scheduled += 1
        return scheduled

    def wait_for_all(self) -> None:
        self._queue.join()

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                self._emit(job.callback, job.directory, "running")
                state = process_experiment(job.directory, job.config_path, job.intrinsics_path)
                self._emit(
                    job.callback,
                    job.directory,
                    "complete",
                    **{key: value for key, value in state.items() if key != "status"},
                )
            except Exception as exc:
                failed_state = {
                    "status": "failed",
                    "error": str(exc),
                    "updated_at": datetime.now().isoformat(timespec="milliseconds"),
                }
                _write_json_atomic(job.directory / STATE_NAME, failed_state)
                self._emit(job.callback, job.directory, "failed", error=str(exc))
            finally:
                with self._lock:
                    self._pending.discard(job.directory)
                self._queue.task_done()

    @staticmethod
    def _emit(
        callback: Callable[[dict], None] | None,
        directory: Path,
        status: str,
        **extra,
    ) -> None:
        if callback is None:
            return
        try:
            callback(
                {
                    "type": "offline_gripper",
                    "status": status,
                    "directory": str(directory),
                    **extra,
                }
            )
        except Exception:
            pass
