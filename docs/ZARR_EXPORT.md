# Zarr Dataset Export

`export_capture_to_zarr.py` converts one or more ARPoseStreamer capture folders into
a Zarr v2 dataset directory compatible with the observed `dataset.zarr` layout.

## Install Dependencies

```bash
pip install -r requirements_visualizer.txt
```

The exporter requires `zarr`, `numcodecs`, `numpy`, `scipy`, and
`opencv-python-headless`.

## Basic Usage

### Desktop UI

Run the UI:

```bash
python zarr_exporter_ui.py
```

Then:

1. Click `Add Capture Folder` and select one or more capture folders.
2. Choose an output path such as `dataset.zarr`.
3. Keep `Zero action` unless downstream code explicitly wants force copied into `action`.
4. Click `Convert`.
5. Click `Open Output Folder` when the conversion finishes.

### Command Line

```bash
python export_capture_to_zarr.py --capture "uploads/20260621-153256" --out "dataset.zarr" --overwrite
```

Multiple captures can be exported into one dataset:

```bash
python export_capture_to_zarr.py \
  --capture "uploads/session-a" \
  --capture "uploads/session-b" \
  --out "dataset.zarr" \
  --overwrite
```

Each capture becomes one episode. `meta/episode_ends` stores cumulative frame
counts, for example `[120, 260]`.

## Output Schema

The exporter writes:

- `data/camera0_rgb`: `uint8`, `[N, 224, 224, 3]`
- `data/timestamp`: `float64`, `[N]`
- `data/robot0_eef_pos`: `float32`, `[N, 3]`
- `data/robot0_eef_rot_axis_angle`: `float32`, `[N, 3]`
- `data/robot0_gripper_width`: `float32`, `[N, 1]`
- `data/robot0_demo_start_pose`: `float32`, `[N, 6]`
- `data/robot0_demo_end_pose`: `float32`, `[N, 6]`
- `data/action`: `float32`, `[N, 7]`
- `data/force_torque`: `float32`, `[N, 6]`
- `data/force_valid`: `bool`, `[N]`
- `data/magnet_xyz`: `float32`, `[N, 2, 4, 3]` (board order: right, left)
- `data/magnet_timestamp_ns`: `int64`, `[N, 2]`
- `data/magnet_sample_count`: `int32`, `[N, 2]`
- `data/magnetic_txyz`: `float32`, `[N, 5, 4]`
- `data/magnetic_valid`: `bool`, `[N, 5]`
- `data/magnetic_left_txyz`: `float32`, `[N, 5, 4]`
- `data/magnetic_left_valid`: `bool`, `[N, 5]`
- `meta/episode_ends`: `int64`, `[episode_count]`

Video is decoded from MP4 into RGB frames. The MP4 itself is not embedded.

## Field Mapping

- `camera0_rgb` is center-cropped/resized video at 224 x 224.
- `robot0_eef_pos` comes from AR pose `x, y, z`.
- `robot0_eef_rot_axis_angle` comes from AR pose quaternion `qx, qy, qz, qw`.
- `demo_start_pose` and `demo_end_pose` repeat the first and last `[pos, rotvec]`.
- `force_torque` stores `[fx, fy, fz, tx, ty, tz]` when a force CSV exists.
- `force_valid` marks frames that were within the force data time range.
- `magnetic_txyz` / `magnetic_valid` retain the right-board data under the
  historical key names.
- `magnetic_left_txyz` / `magnetic_left_valid` store the left-board data.
- `magnet_xyz` exposes both boards to downstream evaluation as sensor sets 0
  (right) and 1 (left). Legacy single-board captures fill the left set with
  zeros and mark its sample count/validity false.
- `action` is zero-filled by default because ARPoseStreamer does not capture true
  7-DoF robot actions.

If downstream code insists on reading `data/action`, you can explicitly copy
force/torque into the first six action dimensions:

```bash
python export_capture_to_zarr.py --capture "uploads/session-a" --out "dataset.zarr" --action-source force --overwrite
```

In that mode, `action[:, 0:6] = [fx, fy, fz, tx, ty, tz]` and `action[:, 6] = 0`.
