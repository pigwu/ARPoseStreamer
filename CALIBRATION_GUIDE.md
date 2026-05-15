# Calibration Guide

Language / 语言: [English Overview](docs/CALIBRATION_OVERVIEW.en.md) | [中文总览](docs/CALIBRATION_OVERVIEW.zh-CN.md)

This file now serves as a clean entry point for the offline calibration workflow.

Recommended reading:

- [Offline Calibration Overview (EN)](docs/CALIBRATION_OVERVIEW.en.md)
- [离线标定总览（简体中文）](docs/CALIBRATION_OVERVIEW.zh-CN.md)

What is covered in those docs:

- pipeline steps
- time synchronization
- kinematic scale initialization and geometric scale refinement
- `AX = XB` hand-eye calibration
- `T_base_world` session alignment
- reconstruction error vs relative-motion consistency
- cross-validation for overfitting risk quantification
- what is reusable across sessions and what is not

Run example:

```powershell
python pose_tracking_validator.py --mode offline --arkit-csv "uploads\20260511-225434\pose_csv__pose.csv" --sensor-csv "uploads\end_effector_pose_log (1).csv" --output-dir offline_calibration_output
```
