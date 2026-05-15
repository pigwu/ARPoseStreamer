# Offline Calibration Overview

Language / 语言: [English](CALIBRATION_OVERVIEW.en.md) | [简体中文](CALIBRATION_OVERVIEW.zh-CN.md)

## What This Pipeline Solves

This offline pipeline aligns ARKit camera poses with robot end-effector poses and estimates:

- `time_shift`: session-level timing alignment between ARKit and robot logs
- `initial_scale_factor`: kinematic scale guess from aligned speed profiles
- `scale_factor`: refined effective scale selected by multi-frame geometric consistency
- `T_cam2gripper = ^gT_c`: fixed hand-eye extrinsic from camera to gripper
- `T_base_world = ^bT_w`: session-level transform from ARKit world to robot base

It is implemented in [pose_tracking_validator.py](../pose_tracking_validator.py).

## Core Principle

The hand-eye part follows the classic eye-in-hand equation:

`A X = X B`

Where:

- `A` is the robot gripper relative motion
- `B` is the camera relative motion
- `X = ^gT_c` is the fixed camera-to-gripper transform

The difference from a chessboard-based pipeline is that we use ARKit pose output instead of image-based target pose estimation. In practice, `ARKit world` plays the role of a session-local target/world frame.

## Pipeline Steps

## How To Run

Example:

```powershell
python pose_tracking_validator.py --mode offline --arkit-csv "uploads\20260511-225434\pose_csv__pose.csv" --sensor-csv "uploads\end_effector_pose_log (1).csv" --output-dir offline_calibration_output
```

Outputs per run:

- one timestamped output folder under `offline_calibration_output/`
- `offline_calibration_result.json`
- velocity, reconstruction, and comparison plots
- cross-validation summary plot when available

### 1. Time Synchronization

- Normalize both timestamps to start at zero.
- Differentiate position with respect to time.
- Convert velocity vectors to scalar speed `v = ||dp/dt||_2`.
- Use active-motion cross-correlation to obtain a coarse `time_shift`.
- Refine `time_shift` by local search on overlapping motion windows.

Why it matters:

- Relative geometry is meaningless if the two streams are misaligned in time.
- This stage uses motion pattern matching, not rigid-body fitting.

### 2. Scale Initialization

- After time synchronization, compare the peak speed magnitude on the overlapping motion window.
- Use that ratio only as a kinematic initialization.

Important:

- This is **not** treated as the final physical truth anymore.
- Camera-center speed and gripper-center speed are not guaranteed to be equal because they are different points on the rigid body.

### 3. Geometric Scale Refinement

- Search around the kinematic scale initialization.
- For every candidate scale:
  - run hand-eye calibration
  - run world-alignment refinement
  - evaluate mean absolute end-effector reconstruction error
- choose the scale that minimizes the multi-frame reconstruction error

This means the final `scale_factor` is the most self-consistent scale under the current rigid model, not a guaranteed external-ground-truth scale.

### 4. Hand-Eye Calibration

- Pair robot frames and ARKit frames using nearest timestamps after time alignment.
- Build robot poses as `^bT_g`.
- Build camera poses as `^wT_c`, with translation multiplied by `scale_factor`.
- Invert ARKit pose to form `^cT_w`, matching OpenCV's `target -> camera` convention.
- Call `cv2.calibrateHandEye(..., method=cv2.CALIB_HAND_EYE_TSAI)`.

Output:

- `T_cam2gripper = ^gT_c`

### 5. World Alignment

- For each matched frame, compute one candidate:

  `^bT_w = ^bT_g · ^gT_c · ^cT_w`

- Average all candidates to obtain the session-level `T_base_world`.

### 6. Optional World Refinement

- Keep `scale_factor` and `T_cam2gripper` fixed.
- Optimize only `T_base_world` to reduce absolute end-effector reconstruction error.

In the latest runs, this step is often negligible if the refined scale already explains the geometry well.

## What “Predicted End-Effector Pose” Means

It does not mean a learned prediction. It means a reconstructed robot pose from the calibrated transform chain:

`^bT_g^pred = ^bT_w · ^wT_c · (^gT_c)^-1`

This is how ARKit camera pose is converted back into robot end-effector pose.

## Error Metrics

### Relative Error

- Computed every 5 matched frames.
- Measures the translational discrepancy between `A X` and `X B`.
- Interpreted as hand-eye relative-motion consistency.

### Absolute Error

- Compares reconstructed end-effector position against robot ground truth.
- Interpreted as end-effector reconstruction accuracy for the current dataset.

## Validation Caveat

Absolute error is currently an **in-sample reconstruction error** unless cross-validation is used.

That means low error proves the model explains the current dataset well, but it does not automatically prove all estimated parameters are physically true in a general sense.

## Cross-Validation Risk Quantification

The project now reports scale cross-validation statistics:

- block-wise train/validation split over matched frame pairs
- best scale on training folds
- validation reconstruction error gap
- improvement over the kinematic scale initialization

This helps quantify overfitting risk instead of assuming the lowest in-sample error is automatically correct.

## What Can Be Reused Across Sessions

- `T_cam2gripper`: reusable only if the phone mounting to the robot gripper is unchanged
- `T_base_world`: session-specific, usually must be recomputed after ARKit world reset or new session start
- `time_shift` / `scale_factor`: session/data specific

## Limitations

- ARKit world is a session-local frame, not a globally persistent external truth
- final scale is model-consistent, not automatically externally proven scale truth
- in-sample error alone can overestimate real-world performance
- if the phone is detached from the robot, this calibration cannot directly map independent phone motion into robot base coordinates

## Recommended Wording For Technical Reports

Use these terms instead of stronger but misleading claims:

- “effective scale estimate” instead of “true metric scale”
- “reconstruction error” instead of “independent validation error”
- “hand-eye consistency” for `AX = XB` residuals
- “session alignment” for `T_base_world`
