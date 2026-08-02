from __future__ import annotations

import bisect
import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


def _float(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


@dataclass
class TimedRows:
    rows: list[dict[str, str]] = field(default_factory=list)
    times: list[float] = field(default_factory=list)

    @classmethod
    def from_csv(cls, path: Path | None, time_columns: Iterable[str]) -> "TimedRows":
        if path is None or not path.is_file():
            return cls()

        rows: list[dict[str, str]] = []
        times: list[float] = []
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                time_value = None
                for column in time_columns:
                    if row.get(column) not in (None, ""):
                        time_value = _float(row[column])
                        break
                if time_value is None:
                    continue
                rows.append(row)
                times.append(time_value)
        return cls(rows=rows, times=times)

    def nearest(self, target_time: float) -> dict[str, str] | None:
        if not self.times:
            return None
        index = bisect.bisect_left(self.times, target_time)
        if index <= 0:
            return self.rows[0]
        if index >= len(self.times):
            return self.rows[-1]
        before = index - 1
        return self.rows[before] if target_time - self.times[before] <= self.times[index] - target_time else self.rows[index]


@dataclass
class ExperimentDataset:
    directory: Path
    manifest: dict
    pose: TimedRows
    # ``magnetic`` remains the right-board table for compatibility with old
    # replay/export code and single-board captures.
    magnetic: TimedRows
    magnetic_left: TimedRows
    sender_transport: TimedRows
    receiver_transport: TimedRows
    gripper: TimedRows
    video_path: Path | None
    ultrawide_video_path: Path | None
    gripper_path: Path | None
    gripper_state: dict
    duration_seconds: float
    video_start_offset_seconds: float
    ultrawide_video_start_offset_seconds: float
    is_complete: bool

    @property
    def experiment_id(self) -> str:
        return str(
            self.manifest.get("experimentID")
            or self.manifest.get("experiment_id")
            or self.directory.name
        )

    @property
    def display_name(self) -> str:
        start = self.manifest.get("experimentStartUnixTime") or self.manifest.get("createdAtUnixTime")
        status = "complete" if self.is_complete else "receiving"
        if start:
            from datetime import datetime

            stamp = datetime.fromtimestamp(_float(start)).strftime("%Y-%m-%d %H:%M:%S")
            return f"{stamp}  [{status}]\n{self.experiment_id}"
        return f"{self.directory.name}  [{status}]"

    @classmethod
    def load(cls, directory: Path) -> "ExperimentDataset":
        directory = directory.expanduser().resolve()
        upload_state = _read_json(directory / "upload_state.json")
        components = upload_state.get("components") if isinstance(upload_state.get("components"), dict) else {}

        manifest_path = _component_path(
            directory,
            components,
            "manifest",
            ["capture_manifest.json", "manifest__capture_manifest.json"],
        )
        manifest = _read_json(manifest_path) if manifest_path else {}

        pose_path = _component_path(directory, components, "pose_csv", ["pose.csv", "pose_csv__pose.csv"])
        magnetic_path = _component_path(
            directory,
            components,
            "magnetic_right_csv",
            ["magnetic_right.csv", "magnetic_right_csv__magnetic_right.csv"],
        )
        if magnetic_path is None:
            magnetic_path = _component_path(
                directory,
                components,
                "magnetic_csv",
                ["magnetic.csv", "magnetic_csv__magnetic.csv"],
            )
        magnetic_left_path = _component_path(
            directory,
            components,
            "magnetic_left_csv",
            ["magnetic_left.csv", "magnetic_left_csv__magnetic_left.csv"],
        )
        sender_path = _component_path(
            directory,
            components,
            "sender_transport",
            ["sender_transport.csv", "sender_transport__sender_transport.csv"],
        )
        receiver_path = _component_path(
            directory,
            components,
            "receiver_transport",
            ["receiver_transport.csv"],
        )
        gripper_path = _component_path(
            directory,
            components,
            "aruco_gripper",
            ["aruco_gripper.csv"],
        )
        video_path = _component_path(directory, components, "video", ["video.mp4", "video.mov"])
        if video_path is None:
            video_candidates = list(directory.glob("video.*")) + list(directory.glob("video__*"))
            video_path = next((path for path in video_candidates if path.suffix.lower() in {".mp4", ".mov", ".m4v"}), None)
        ultrawide_video_path = _component_path(
            directory,
            components,
            "ultrawide_video",
            ["ultrawide_video.mp4", "ultrawide_video.mov"],
        )
        if ultrawide_video_path is None:
            ultrawide_candidates = list(directory.glob("ultrawide_video.*")) + list(
                directory.glob("ultrawide_video__*")
            )
            ultrawide_video_path = next(
                (
                    path
                    for path in ultrawide_candidates
                    if path.suffix.lower() in {".mp4", ".mov", ".m4v"}
                ),
                None,
            )

        start_monotonic = _float(
            manifest.get("experimentStartMonotonicTime", manifest.get("sessionStartFrameTime", 0.0))
        )
        start_unix = _float(manifest.get("experimentStartUnixTime", manifest.get("createdAtUnixTime", 0.0)))

        pose = TimedRows.from_csv(pose_path, ["relative_time", "experiment_time"])
        magnetic = TimedRows.from_csv(
            magnetic_path,
            ["relative_time", "experiment_time", "phone_monotonic_time"],
        )
        magnetic_left = TimedRows.from_csv(
            magnetic_left_path,
            ["relative_time", "experiment_time", "phone_monotonic_time"],
        )
        for table in (magnetic, magnetic_left):
            if table.rows and "relative_time" not in table.rows[0]:
                table.times = [
                    max(0.0, _float(row.get("phone_monotonic_time")) - start_monotonic)
                    for row in table.rows
                ]
        sender_transport = TimedRows.from_csv(sender_path, ["relative_time", "experiment_time"])
        receiver_transport = TimedRows.from_csv(
            receiver_path,
            ["experiment_time", "relative_time", "sender_time"],
        )
        if receiver_transport.rows and not any(
            key in receiver_transport.rows[0] for key in ("experiment_time", "relative_time")
        ):
            receiver_transport.times = [
                max(0.0, _float(row.get("sender_time")) - start_unix)
                for row in receiver_transport.rows
            ]
        gripper = TimedRows.from_csv(gripper_path, ["experiment_time", "relative_time"])

        duration = _float(manifest.get("durationSeconds"), 0.0)
        for table in (
            pose,
            magnetic,
            magnetic_left,
            sender_transport,
            receiver_transport,
            gripper,
        ):
            if table.times:
                duration = max(duration, table.times[-1])

        return cls(
            directory=directory,
            manifest=manifest,
            pose=pose,
            magnetic=magnetic,
            magnetic_left=magnetic_left,
            sender_transport=sender_transport,
            receiver_transport=receiver_transport,
            gripper=gripper,
            video_path=video_path,
            ultrawide_video_path=ultrawide_video_path,
            gripper_path=gripper_path,
            gripper_state=_read_json(directory / "aruco_gripper_state.json"),
            duration_seconds=max(duration, 0.001),
            video_start_offset_seconds=_float(manifest.get("videoStartOffsetSeconds"), 0.0),
            ultrawide_video_start_offset_seconds=_float(
                manifest.get("ultraWideVideoStartOffsetSeconds"),
                0.0,
            ),
            is_complete=(bool(upload_state.get("complete")) if upload_state else bool(manifest)),
        )


def discover_experiments(root: Path) -> list[ExperimentDataset]:
    root = root.expanduser().resolve()
    if not root.exists():
        return []

    datasets: list[ExperimentDataset] = []
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        if not any(
            (directory / filename).exists()
            for filename in ("capture_manifest.json", "manifest__capture_manifest.json", "upload_state.json", "experiment_state.json")
        ):
            continue
        try:
            datasets.append(ExperimentDataset.load(directory))
        except (OSError, csv.Error, json.JSONDecodeError):
            continue

    datasets.sort(
        key=lambda dataset: _float(
            dataset.manifest.get("experimentStartUnixTime", dataset.manifest.get("createdAtUnixTime", 0.0))
        ),
        reverse=True,
    )
    return datasets


def _read_json(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _component_path(
    directory: Path,
    components: dict,
    component: str,
    fallback_names: list[str],
) -> Path | None:
    state_name = components.get(component)
    if isinstance(state_name, str):
        candidate = directory / Path(state_name).name
        if candidate.is_file():
            return candidate
    for name in fallback_names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    wildcard = next(directory.glob(f"{component}__*"), None)
    return wildcard if wildcard and wildcard.is_file() else None
