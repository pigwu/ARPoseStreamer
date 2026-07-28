from __future__ import annotations

import itertools
import json
import queue
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import zarr

from export_capture_to_zarr import (
    RDP_SOURCE_ZARR_SCHEMA_VERSION,
    build_episode,
    default_eef_calibration_result,
    discover_capture,
    make_zarr_attrs,
    write_zarr,
)


ZARR_DIRECTORY_NAME = "dataset.zarr"
ZARR_STATE_NAME = "zarr_state.json"
REQUIRED_RDP_DATA_KEYS = (
    "camera0_rgb",
    "robot0_eef_pos",
    "robot0_eef_rot_axis_angle",
    "robot0_gripper_width",
    "robot0_demo_start_pose",
    "robot0_demo_end_pose",
    "action",
    "magnet_xyz",
    "magnet_timestamp_ns",
    "magnet_sample_count",
    "timestamp",
)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_json_atomic(path: Path, value: dict) -> None:
    temp_path = path.with_suffix(path.suffix + ".part")
    temp_path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(path)


def is_current_rdp_source_zarr(path: Path) -> bool:
    try:
        root = zarr.open_group(str(path), mode="r")
        if root.attrs.get("rdp_source_zarr_schema_version") != RDP_SOURCE_ZARR_SCHEMA_VERSION:
            return False
        if "data" not in root:
            return False
        data = root["data"]
        if any(key not in data for key in REQUIRED_RDP_DATA_KEYS):
            return False
        if data["action"].ndim != 2 or data["action"].shape[-1] != 7:
            return False
        if data["magnet_xyz"].ndim != 4 or data["magnet_xyz"].shape[-1] != 3:
            return False
        return True
    except Exception:
        return False


class AutoZarrExporter:
    """Convert completed experiments sequentially without blocking uploads."""

    def __init__(self, image_size: int = 224, eef_calibration_result: Optional[Path] = None) -> None:
        self.image_size = image_size
        self.eef_calibration_result = (
            Path(eef_calibration_result).expanduser().resolve()
            if eef_calibration_result is not None
            else default_eef_calibration_result()
        )
        self._queue: queue.PriorityQueue[tuple[int, int, Path, str, Optional[Callable[[dict], None]]]] = (
            queue.PriorityQueue()
        )
        self._counter = itertools.count()
        self._lock = threading.Lock()
        self._pending: set[Path] = set()
        self._worker: threading.Thread | None = None

    def schedule(
        self,
        directory: Path,
        capture_id: str,
        on_event: Optional[Callable[[dict], None]] = None,
        *,
        priority: int = 0,
    ) -> bool:
        directory = directory.expanduser().resolve()
        output = directory / ZARR_DIRECTORY_NAME
        state_path = directory / ZARR_STATE_NAME
        state = read_json(state_path)
        if output.is_dir() and state.get("status") == "complete" and is_current_rdp_source_zarr(output):
            return False

        with self._lock:
            if directory in self._pending:
                return False
            self._pending.add(directory)
            write_json_atomic(
                state_path,
                {
                    "status": "queued",
                    "capture_id": capture_id,
                    "output": ZARR_DIRECTORY_NAME,
                    "eef_calibration_result": str(self.eef_calibration_result or ""),
                    "updated_at": datetime.now().isoformat(timespec="milliseconds"),
                },
            )
            self._queue.put((priority, next(self._counter), directory, capture_id, on_event))
            self._ensure_worker_locked()

        self._emit(on_event, directory, capture_id, "queued")
        return True

    def backfill(
        self,
        root: Path,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> int:
        root = root.expanduser().resolve()
        if not root.is_dir():
            return 0
        scheduled = 0
        for directory in sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name):
            upload_state = read_json(directory / "upload_state.json")
            if upload_state and not upload_state.get("complete"):
                continue
            has_manifest = any(
                (directory / name).is_file()
                for name in ("capture_manifest.json", "manifest__capture_manifest.json")
            )
            if not has_manifest:
                continue
            capture_id = str(
                upload_state.get("capture_id")
                or read_json(directory / "experiment_state.json").get("experiment_id")
                or directory.name
            )
            if self.schedule(directory, capture_id, on_event, priority=10):
                scheduled += 1
        return scheduled

    def wait_for_all(self) -> None:
        """Wait until every queued conversion has finished."""
        self._queue.join()

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, name="arpose-zarr-export", daemon=True)
        self._worker.start()

    def _run(self) -> None:
        while True:
            _priority, _order, directory, capture_id, on_event = self._queue.get()
            try:
                self._convert(directory, capture_id, on_event)
            finally:
                with self._lock:
                    self._pending.discard(directory)
                self._queue.task_done()

    def _convert(
        self,
        directory: Path,
        capture_id: str,
        on_event: Optional[Callable[[dict], None]],
    ) -> None:
        state_path = directory / ZARR_STATE_NAME
        output = directory / ZARR_DIRECTORY_NAME
        temporary_output = directory / f"{ZARR_DIRECTORY_NAME}.part"
        state = {
            "status": "running",
            "capture_id": capture_id,
            "output": ZARR_DIRECTORY_NAME,
            "eef_calibration_result": str(self.eef_calibration_result or ""),
            "updated_at": datetime.now().isoformat(timespec="milliseconds"),
        }
        write_json_atomic(state_path, state)
        self._emit(on_event, directory, capture_id, "running")

        try:
            if temporary_output.exists():
                shutil.rmtree(temporary_output)
            capture = discover_capture(directory)
            episode = build_episode(
                capture,
                image_size=self.image_size,
                action_source="next_obs",
                eef_calibration_result=self.eef_calibration_result,
            )
            attrs = make_zarr_attrs(self.eef_calibration_result, action_source="next_obs")
            attrs.update(
                {
                    "capture_id": capture_id,
                    "source_directory": directory.name,
                    "source_manifest": capture.manifest,
                    "created_by": "ARPose Experiment Monitor",
                    "rdp_source_zarr_schema_version": RDP_SOURCE_ZARR_SCHEMA_VERSION,
                    "magnet_key": "magnet_xyz",
                }
            )
            write_zarr(temporary_output, [episode], attrs=attrs)
            if output.exists():
                shutil.rmtree(output)
            temporary_output.rename(output)
            state.update(
                {
                    "status": "complete",
                    "frames": int(len(episode.timestamp)),
                    "eef_pose_source": attrs["eef_pose_source"],
                    "eef_calibration_result": attrs["eef_calibration_result"],
                    "updated_at": datetime.now().isoformat(timespec="milliseconds"),
                }
            )
            write_json_atomic(state_path, state)
            self._emit(on_event, directory, capture_id, "complete", frames=state["frames"])
        except Exception as exc:
            if temporary_output.exists():
                shutil.rmtree(temporary_output, ignore_errors=True)
            state.update(
                {
                    "status": "failed",
                    "error": str(exc),
                    "updated_at": datetime.now().isoformat(timespec="milliseconds"),
                }
            )
            write_json_atomic(state_path, state)
            self._emit(on_event, directory, capture_id, "failed", error=str(exc))

    @staticmethod
    def _emit(
        callback: Optional[Callable[[dict], None]],
        directory: Path,
        capture_id: str,
        status: str,
        **extra,
    ) -> None:
        if callback is None:
            return
        try:
            callback(
                {
                    "type": "zarr",
                    "status": status,
                    "capture_id": capture_id,
                    "directory": str(directory),
                    "output": str(directory / ZARR_DIRECTORY_NAME),
                    **extra,
                }
            )
        except Exception:
            pass
