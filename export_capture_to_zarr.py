import argparse
import csv
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import cv2
import numpy as np
import zarr
from numcodecs import Blosc
from scipy.spatial.transform import Rotation, Slerp


IMAGE_SIZE = 224
POSE_DIM = 6
ACTION_DIM = 7
FORCE_DIM = 6
MAGNETIC_CHIP_COUNT = 5
MAGNETIC_VALUE_DIM = 4
ZARR_CHUNK_ROWS = 1024


@dataclass
class CaptureInputs:
    directory: Path
    manifest_path: Optional[Path]
    manifest: dict
    pose_csv: Optional[Path]
    magnetic_csv: Optional[Path]
    force_csv: Optional[Path]
    video_path: Optional[Path]


@dataclass
class PoseSeries:
    time: np.ndarray
    position: np.ndarray
    quaternion: np.ndarray


@dataclass
class ForceSeries:
    time: np.ndarray
    values: np.ndarray


@dataclass
class MagneticSeries:
    time: np.ndarray
    values: np.ndarray


@dataclass
class EpisodeArrays:
    camera_rgb: np.ndarray
    timestamp: np.ndarray
    eef_pos: np.ndarray
    eef_rot_axis_angle: np.ndarray
    gripper_width: np.ndarray
    demo_start_pose: np.ndarray
    demo_end_pose: np.ndarray
    action: np.ndarray
    force_torque: np.ndarray
    force_valid: np.ndarray
    magnetic_txyz: np.ndarray
    magnetic_valid: np.ndarray


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert ARPoseStreamer capture folders into a dataset.zarr directory."
    )
    parser.add_argument(
        "--capture",
        action="append",
        required=True,
        type=Path,
        help="Capture folder. Can be passed multiple times for multiple episodes.",
    )
    parser.add_argument("--out", required=True, type=Path, help="Output .zarr directory.")
    parser.add_argument(
        "--image-size",
        type=int,
        default=IMAGE_SIZE,
        help="Square RGB image size written to camera0_rgb. Default: 224.",
    )
    parser.add_argument(
        "--action-source",
        choices=("zero", "force"),
        default="zero",
        help="Write action as zeros, or copy force/torque into action[:,0:6]. Default: zero.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Delete the output directory if it already exists.",
    )
    args = parser.parse_args()

    if args.out.exists():
        if not args.overwrite:
            raise SystemExit(f"Output already exists: {args.out}. Use --overwrite to replace it.")
        shutil.rmtree(args.out)

    captures = [discover_capture(path) for path in args.capture]
    episodes = [
        build_episode(capture, image_size=args.image_size, action_source=args.action_source)
        for capture in captures
    ]
    write_zarr(args.out, episodes)

    total_frames = sum(len(ep.timestamp) for ep in episodes)
    print(f"Wrote {args.out} with {len(episodes)} episode(s), {total_frames} frame(s).")
    return 0


def discover_capture(directory: Path) -> CaptureInputs:
    directory = directory.resolve()
    if not directory.is_dir():
        raise FileNotFoundError(f"Capture directory not found: {directory}")

    manifest_path = first_existing(
        [
            directory / "capture_manifest.json",
            directory / "manifest__capture_manifest.json",
            *sorted(directory.glob("*manifest*.json")),
        ]
    )
    manifest = load_json(manifest_path) if manifest_path else {}

    pose_name = manifest.get("poseCSVFileName")
    pose_csv = find_capture_file(
        directory,
        logical_name=pose_name,
        component_prefix="pose_csv",
        fallback_patterns=("pose.csv", "pose_csv__*.csv", "*pose*.csv"),
        require_nonempty=True,
    )

    magnetic_name = manifest.get("magneticCSVFileName")
    magnetic_csv = find_capture_file(
        directory,
        logical_name=magnetic_name,
        component_prefix="magnetic_csv",
        fallback_patterns=("magnetic.csv", "magnetic_csv__*.csv", "*magnetic*.csv"),
        require_nonempty=True,
        required=False,
    )

    force_csv = find_capture_file(
        directory,
        logical_name=None,
        component_prefix=None,
        fallback_patterns=("fused_force_pose.csv", "force.csv", "*force*.csv"),
        require_nonempty=True,
        required=False,
    )

    video_name = manifest.get("videoFileName")
    video_path = find_capture_file(
        directory,
        logical_name=video_name,
        component_prefix="video",
        fallback_patterns=("*.mp4", "*.mov"),
        require_nonempty=True,
        required=False,
    )

    return CaptureInputs(
        directory=directory,
        manifest_path=manifest_path,
        manifest=manifest,
        pose_csv=pose_csv,
        magnetic_csv=magnetic_csv,
        force_csv=force_csv,
        video_path=video_path,
    )


def build_episode(capture: CaptureInputs, image_size: int, action_source: str) -> EpisodeArrays:
    pose = load_pose_series(capture.pose_csv, capture.manifest) if capture.pose_csv else None
    magnetic = load_magnetic_series(capture.magnetic_csv, capture.manifest) if capture.magnetic_csv else None
    force = load_force_series(capture.force_csv, capture.manifest) if capture.force_csv else None

    camera_rgb, sample_time = load_video_frames(
        capture.video_path,
        capture.manifest,
        fallback_pose=pose,
        image_size=image_size,
    )

    if len(sample_time) == 0:
        raise ValueError(f"No video frames or pose samples available in {capture.directory}")

    eef_pos, eef_rot_axis_angle = sample_pose_on_time(pose, sample_time)
    force_torque, force_valid = sample_force_on_time(force, sample_time)
    magnetic_txyz, magnetic_valid = sample_magnetic_on_time(magnetic, sample_time)

    pose6 = np.concatenate([eef_pos, eef_rot_axis_angle], axis=1).astype(np.float32)
    start_pose = pose6[0] if len(pose6) else np.zeros(POSE_DIM, dtype=np.float32)
    end_pose = pose6[-1] if len(pose6) else np.zeros(POSE_DIM, dtype=np.float32)
    demo_start_pose = np.repeat(start_pose[None, :], len(sample_time), axis=0).astype(np.float32)
    demo_end_pose = np.repeat(end_pose[None, :], len(sample_time), axis=0).astype(np.float32)

    action = np.zeros((len(sample_time), ACTION_DIM), dtype=np.float32)
    if action_source == "force":
        action[:, :FORCE_DIM] = force_torque

    return EpisodeArrays(
        camera_rgb=camera_rgb.astype(np.uint8, copy=False),
        timestamp=sample_time.astype(np.float64, copy=False),
        eef_pos=eef_pos.astype(np.float32, copy=False),
        eef_rot_axis_angle=eef_rot_axis_angle.astype(np.float32, copy=False),
        gripper_width=np.zeros((len(sample_time), 1), dtype=np.float32),
        demo_start_pose=demo_start_pose,
        demo_end_pose=demo_end_pose,
        action=action,
        force_torque=force_torque.astype(np.float32, copy=False),
        force_valid=force_valid.astype(bool, copy=False),
        magnetic_txyz=magnetic_txyz.astype(np.float32, copy=False),
        magnetic_valid=magnetic_valid.astype(bool, copy=False),
    )


def load_video_frames(
    video_path: Optional[Path],
    manifest: dict,
    fallback_pose: Optional[PoseSeries],
    image_size: int,
) -> Tuple[np.ndarray, np.ndarray]:
    if video_path is None:
        if fallback_pose is None or len(fallback_pose.time) == 0:
            return np.zeros((0, image_size, image_size, 3), dtype=np.uint8), np.zeros(0, dtype=np.float64)
        frames = np.zeros((len(fallback_pose.time), image_size, image_size, 3), dtype=np.uint8)
        return frames, fallback_pose.time.copy()

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"Could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    if not np.isfinite(fps) or fps <= 1e-6:
        fps = 60.0

    video_start_offset = float(manifest.get("videoStartOffsetSeconds") or 0.0)
    created_at = manifest.get("createdAtUnixTime")
    created_at = float(created_at) if created_at is not None else None

    frames: List[np.ndarray] = []
    times: List[float] = []
    frame_index = 0

    while True:
        ok, bgr = cap.read()
        if not ok:
            break

        pos_msec = cap.get(cv2.CAP_PROP_POS_MSEC)
        if np.isfinite(pos_msec) and pos_msec >= 0:
            frame_time = pos_msec / 1000.0
        else:
            frame_time = frame_index / fps

        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        rgb = resize_center_crop(rgb, image_size)
        frames.append(rgb)

        relative_time = video_start_offset + frame_time
        times.append((created_at + relative_time) if created_at is not None else relative_time)
        frame_index += 1

    cap.release()

    if not frames:
        raise ValueError(f"Video had no readable frames: {video_path}")

    return np.stack(frames, axis=0), np.asarray(times, dtype=np.float64)


def resize_center_crop(image: np.ndarray, output_size: int) -> np.ndarray:
    height, width = image.shape[:2]
    scale = output_size / min(height, width)
    resized_width = max(output_size, int(round(width * scale)))
    resized_height = max(output_size, int(round(height * scale)))
    resized = cv2.resize(image, (resized_width, resized_height), interpolation=cv2.INTER_AREA)

    y0 = max(0, (resized_height - output_size) // 2)
    x0 = max(0, (resized_width - output_size) // 2)
    return resized[y0 : y0 + output_size, x0 : x0 + output_size]


def load_pose_series(path: Path, manifest: dict) -> PoseSeries:
    rows = read_csv_rows(path)
    if not rows:
        raise ValueError(f"Pose CSV has no rows: {path}")

    created_at = optional_float(manifest.get("createdAtUnixTime"))
    session_start_frame_time = optional_float(manifest.get("sessionStartFrameTime"))

    times = []
    positions = []
    quaternions = []

    for row in rows:
        relative_time = first_float(row, ("relative_time", "time"))
        frame_time = first_float(row, ("frame_time",), required=False)
        sender_time = first_float(row, ("sender_time", "timestamp", "received_time"), required=False)

        if relative_time is None and frame_time is not None and session_start_frame_time is not None:
            relative_time = frame_time - session_start_frame_time

        if created_at is not None and relative_time is not None:
            time_value = created_at + relative_time
        elif sender_time is not None:
            time_value = sender_time
        elif relative_time is not None:
            time_value = relative_time
        else:
            continue

        position = [
            first_float(row, ("x", "px", "pos_x")),
            first_float(row, ("y", "py", "pos_y")),
            first_float(row, ("z", "pz", "pos_z")),
        ]
        quaternion = [
            first_float(row, ("qx",)),
            first_float(row, ("qy",)),
            first_float(row, ("qz",)),
            first_float(row, ("qw",)),
        ]

        if any(value is None for value in position + quaternion):
            continue

        times.append(time_value)
        positions.append(position)
        quaternions.append(normalize_quaternion(quaternion))

    if not times:
        raise ValueError(f"Pose CSV did not contain usable pose rows: {path}")

    order = np.argsort(np.asarray(times, dtype=np.float64))
    return PoseSeries(
        time=np.asarray(times, dtype=np.float64)[order],
        position=np.asarray(positions, dtype=np.float64)[order],
        quaternion=np.asarray(quaternions, dtype=np.float64)[order],
    )


def sample_pose_on_time(pose: Optional[PoseSeries], sample_time: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    if pose is None or len(pose.time) == 0:
        return (
            np.zeros((len(sample_time), 3), dtype=np.float32),
            np.zeros((len(sample_time), 3), dtype=np.float32),
        )

    unique_time, unique_indices = np.unique(pose.time, return_index=True)
    positions = pose.position[unique_indices]
    quaternions = pose.quaternion[unique_indices]

    if len(unique_time) == 1:
        sampled_pos = np.repeat(positions[0][None, :], len(sample_time), axis=0)
        sampled_quat = np.repeat(quaternions[0][None, :], len(sample_time), axis=0)
    else:
        clipped_time = np.clip(sample_time, unique_time[0], unique_time[-1])
        sampled_pos = np.column_stack(
            [np.interp(clipped_time, unique_time, positions[:, axis]) for axis in range(3)]
        )
        rotations = Rotation.from_quat(quaternions)
        slerp = Slerp(unique_time, rotations)
        sampled_quat = slerp(clipped_time).as_quat()

    rotvec = Rotation.from_quat(sampled_quat).as_rotvec()
    return sampled_pos.astype(np.float32), rotvec.astype(np.float32)


def load_magnetic_series(path: Optional[Path], manifest: dict) -> Optional[MagneticSeries]:
    if path is None:
        return None
    rows = read_csv_rows(path)
    if not rows:
        return None

    start_unix = optional_float(
        manifest.get("experimentStartUnixTime", manifest.get("createdAtUnixTime"))
    )
    start_monotonic = optional_float(
        manifest.get("experimentStartMonotonicTime", manifest.get("sessionStartFrameTime"))
    )
    times = []
    samples = []
    for row in rows:
        relative_time = first_float(row, ("relative_time", "experiment_time"), required=False)
        receive_time = first_float(row, ("phone_receive_time", "sender_time"), required=False)
        monotonic_time = first_float(row, ("phone_monotonic_time",), required=False)
        if start_unix is not None and relative_time is not None:
            time_value = start_unix + relative_time
        elif receive_time is not None:
            time_value = receive_time
        elif start_unix is not None and start_monotonic is not None and monotonic_time is not None:
            time_value = start_unix + monotonic_time - start_monotonic
        elif relative_time is not None:
            time_value = relative_time
        else:
            continue

        chip_values = []
        for chip in range(MAGNETIC_CHIP_COUNT):
            chip_values.append(
                [
                    first_float(row, (f"s{chip}_t",), required=False),
                    first_float(row, (f"s{chip}_x",), required=False),
                    first_float(row, (f"s{chip}_y",), required=False),
                    first_float(row, (f"s{chip}_z",), required=False),
                ]
            )
        values = np.asarray(
            [[np.nan if value is None else value for value in chip] for chip in chip_values],
            dtype=np.float64,
        )
        if not np.isfinite(values).any():
            continue
        times.append(time_value)
        samples.append(values)

    if not times:
        return None
    order = np.argsort(np.asarray(times, dtype=np.float64))
    return MagneticSeries(
        time=np.asarray(times, dtype=np.float64)[order],
        values=np.asarray(samples, dtype=np.float64)[order],
    )


def sample_magnetic_on_time(
    magnetic: Optional[MagneticSeries],
    sample_time: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    values = np.zeros(
        (len(sample_time), MAGNETIC_CHIP_COUNT, MAGNETIC_VALUE_DIM),
        dtype=np.float32,
    )
    valid = np.zeros((len(sample_time), MAGNETIC_CHIP_COUNT), dtype=bool)
    if magnetic is None or len(magnetic.time) == 0:
        return values, valid

    unique_time, unique_indices = np.unique(magnetic.time, return_index=True)
    source = magnetic.values[unique_indices]
    for chip in range(MAGNETIC_CHIP_COUNT):
        finite_rows = np.all(np.isfinite(source[:, chip, :]), axis=1)
        chip_time = unique_time[finite_rows]
        chip_values = source[finite_rows, chip, :]
        if len(chip_time) == 0:
            continue
        if len(chip_time) == 1:
            nearest = np.isclose(sample_time, chip_time[0], atol=1e-3)
            values[nearest, chip, :] = chip_values[0]
            valid[nearest, chip] = True
            continue
        in_range = (sample_time >= chip_time[0]) & (sample_time <= chip_time[-1])
        for axis in range(MAGNETIC_VALUE_DIM):
            values[in_range, chip, axis] = np.interp(
                sample_time[in_range],
                chip_time,
                chip_values[:, axis],
            )
        valid[in_range, chip] = True
    return values, valid


def load_force_series(path: Optional[Path], manifest: dict) -> Optional[ForceSeries]:
    if path is None:
        return None

    rows = read_csv_rows(path)
    if not rows:
        return None

    created_at = optional_float(manifest.get("createdAtUnixTime"))
    times = []
    values = []

    for row in rows:
        force = [
            first_float(row, ("fx", "force_x", "f_x", "force0", "force_0"), required=False),
            first_float(row, ("fy", "force_y", "f_y", "force1", "force_1"), required=False),
            first_float(row, ("fz", "force_z", "f_z", "force2", "force_2"), required=False),
            first_float(row, ("tx", "torque_x", "t_x", "mx", "moment_x", "force3", "force_3"), required=False),
            first_float(row, ("ty", "torque_y", "t_y", "my", "moment_y", "force4", "force_4"), required=False),
            first_float(row, ("tz", "torque_z", "t_z", "mz", "moment_z", "force5", "force_5"), required=False),
        ]
        if any(value is None for value in force):
            continue

        time_value = first_float(
            row,
            (
                "phone_receive_time",
                "received_time",
                "receive_time",
                "force_time",
                "aligned_time",
                "timestamp",
                "time",
                "relative_time",
            ),
            required=False,
        )
        if time_value is None:
            continue

        if created_at is not None and time_value < 1e8:
            time_value = created_at + time_value

        times.append(time_value)
        values.append(force)

    if not times:
        return None

    order = np.argsort(np.asarray(times, dtype=np.float64))
    return ForceSeries(
        time=np.asarray(times, dtype=np.float64)[order],
        values=np.asarray(values, dtype=np.float64)[order],
    )


def sample_force_on_time(force: Optional[ForceSeries], sample_time: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    values = np.zeros((len(sample_time), FORCE_DIM), dtype=np.float32)
    valid = np.zeros(len(sample_time), dtype=bool)

    if force is None or len(force.time) == 0:
        return values, valid

    unique_time, unique_indices = np.unique(force.time, return_index=True)
    force_values = force.values[unique_indices]

    if len(unique_time) == 1:
        nearest = np.isclose(sample_time, unique_time[0], atol=1e-3)
        values[nearest] = force_values[0]
        valid[nearest] = True
        return values, valid

    in_range = (sample_time >= unique_time[0]) & (sample_time <= unique_time[-1])
    for axis in range(FORCE_DIM):
        values[in_range, axis] = np.interp(sample_time[in_range], unique_time, force_values[:, axis])
    valid[in_range] = True
    return values, valid


def write_zarr(out_path: Path, episodes: List[EpisodeArrays]) -> None:
    if not episodes:
        raise ValueError("No episodes to write.")

    camera_rgb = np.concatenate([ep.camera_rgb for ep in episodes], axis=0)
    timestamp = np.concatenate([ep.timestamp for ep in episodes], axis=0)
    eef_pos = np.concatenate([ep.eef_pos for ep in episodes], axis=0)
    eef_rot_axis_angle = np.concatenate([ep.eef_rot_axis_angle for ep in episodes], axis=0)
    gripper_width = np.concatenate([ep.gripper_width for ep in episodes], axis=0)
    demo_start_pose = np.concatenate([ep.demo_start_pose for ep in episodes], axis=0)
    demo_end_pose = np.concatenate([ep.demo_end_pose for ep in episodes], axis=0)
    action = np.concatenate([ep.action for ep in episodes], axis=0)
    force_torque = np.concatenate([ep.force_torque for ep in episodes], axis=0)
    force_valid = np.concatenate([ep.force_valid for ep in episodes], axis=0)
    magnetic_txyz = np.concatenate([ep.magnetic_txyz for ep in episodes], axis=0)
    magnetic_valid = np.concatenate([ep.magnetic_valid for ep in episodes], axis=0)
    episode_ends = np.cumsum([len(ep.timestamp) for ep in episodes], dtype=np.int64)

    compressor = Blosc(cname="zstd", clevel=5, shuffle=Blosc.BITSHUFFLE)

    root = zarr.open_group(str(out_path), mode="w")
    data_group = root.create_group("data")
    meta_group = root.create_group("meta")

    write_array(data_group, "camera0_rgb", camera_rgb, (1, camera_rgb.shape[1], camera_rgb.shape[2], 3), compressor)
    write_array(data_group, "timestamp", timestamp, (ZARR_CHUNK_ROWS,), compressor)
    write_array(data_group, "robot0_eef_pos", eef_pos, (ZARR_CHUNK_ROWS, 3), compressor)
    write_array(data_group, "robot0_eef_rot_axis_angle", eef_rot_axis_angle, (ZARR_CHUNK_ROWS, 3), compressor)
    write_array(data_group, "robot0_gripper_width", gripper_width, (ZARR_CHUNK_ROWS, 1), compressor)
    write_array(data_group, "robot0_demo_start_pose", demo_start_pose, (ZARR_CHUNK_ROWS, POSE_DIM), compressor)
    write_array(data_group, "robot0_demo_end_pose", demo_end_pose, (ZARR_CHUNK_ROWS, POSE_DIM), compressor)
    write_array(data_group, "action", action, (ZARR_CHUNK_ROWS, ACTION_DIM), compressor)
    write_array(data_group, "force_torque", force_torque, (ZARR_CHUNK_ROWS, FORCE_DIM), compressor)
    write_array(data_group, "force_valid", force_valid, (ZARR_CHUNK_ROWS,), compressor)
    write_array(
        data_group,
        "magnetic_txyz",
        magnetic_txyz,
        (ZARR_CHUNK_ROWS, MAGNETIC_CHIP_COUNT, MAGNETIC_VALUE_DIM),
        compressor,
    )
    write_array(
        data_group,
        "magnetic_valid",
        magnetic_valid,
        (ZARR_CHUNK_ROWS, MAGNETIC_CHIP_COUNT),
        compressor,
    )
    write_array(meta_group, "episode_ends", episode_ends, (len(episode_ends),), None)


def write_array(group, name: str, values: np.ndarray, chunks: Tuple[int, ...], compressor) -> None:
    group.create_dataset(
        name,
        data=values,
        shape=values.shape,
        chunks=chunks,
        dtype=values.dtype,
        compressor=compressor,
        overwrite=True,
    )


def find_capture_file(
    directory: Path,
    logical_name: Optional[str],
    component_prefix: Optional[str],
    fallback_patterns: Iterable[str],
    require_nonempty: bool,
    required: bool = True,
) -> Optional[Path]:
    candidates: List[Path] = []

    if logical_name:
        candidates.append(directory / logical_name)
        if component_prefix:
            candidates.append(directory / f"{component_prefix}__{logical_name}")

    for pattern in fallback_patterns:
        candidates.extend(sorted(directory.glob(pattern)))

    for candidate in unique_paths(candidates):
        if candidate.is_file() and (not require_nonempty or candidate.stat().st_size > 0):
            return candidate

    if required:
        raise FileNotFoundError(f"Could not find required file in {directory}: {', '.join(fallback_patterns)}")
    return None


def first_existing(paths: Iterable[Path]) -> Optional[Path]:
    for path in unique_paths(paths):
        if path.is_file():
            return path
    return None


def unique_paths(paths: Iterable[Path]) -> List[Path]:
    seen = set()
    result = []
    for path in paths:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return [{normalize_key(key): value for key, value in row.items()} for row in reader]


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalize_key(key: Optional[str]) -> str:
    return (key or "").strip().lower()


def optional_float(value) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def first_float(row: Dict[str, str], names: Iterable[str], required: bool = True) -> Optional[float]:
    for name in names:
        value = optional_float(row.get(normalize_key(name)))
        if value is not None:
            return value
    if required:
        raise ValueError(f"Missing numeric CSV field. Tried: {', '.join(names)}")
    return None


def normalize_quaternion(values: Iterable[float]) -> List[float]:
    quat = np.asarray(list(values), dtype=np.float64)
    norm = np.linalg.norm(quat)
    if not np.isfinite(norm) or norm <= 1e-12:
        return [0.0, 0.0, 0.0, 1.0]
    return (quat / norm).tolist()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
