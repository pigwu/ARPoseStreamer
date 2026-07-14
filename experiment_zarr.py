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

from export_capture_to_zarr import build_episode, discover_capture, write_zarr


ZARR_DIRECTORY_NAME = "dataset.zarr"
ZARR_STATE_NAME = "zarr_state.json"


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


class AutoZarrExporter:
    """Convert completed experiments sequentially without blocking uploads."""

    def __init__(self, image_size: int = 224) -> None:
        self.image_size = image_size
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
        if output.is_dir() and state.get("status") == "complete":
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
            "updated_at": datetime.now().isoformat(timespec="milliseconds"),
        }
        write_json_atomic(state_path, state)
        self._emit(on_event, directory, capture_id, "running")

        try:
            if temporary_output.exists():
                shutil.rmtree(temporary_output)
            capture = discover_capture(directory)
            episode = build_episode(capture, image_size=self.image_size, action_source="zero")
            write_zarr(temporary_output, [episode])
            root = zarr.open_group(str(temporary_output), mode="a")
            root.attrs.update(
                {
                    "capture_id": capture_id,
                    "source_directory": directory.name,
                    "source_manifest": capture.manifest,
                    "created_by": "ARPose Experiment Monitor",
                }
            )
            if output.exists():
                shutil.rmtree(output)
            temporary_output.rename(output)
            state.update(
                {
                    "status": "complete",
                    "frames": int(len(episode.timestamp)),
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
