# ArUco 夹爪逐帧距离测量

## 功能范围

本功能只做一件事：在每个视频帧中检测夹爪两侧的 ArUco ID 0 和 ID 1，并输出夹爪实际开口距离。

不使用也不需要：

- 机械臂型号或关节数据
- TCP 位姿
- `marker→tool` 外参
- `T_base_world` 外参
- ARKit world/base 位姿融合

## 标记安装

1. 将 PDF 按 100% 原始尺寸打印，不要“适合页面”。
2. 确认字典为 `DICT_4X4_50`，标记 ID 为 `0` 和 `1`。
3. 实测每个标记的黑色外边长，应为 `16.000 mm`。
4. ID 0、ID 1 分别刚性固定在两个活动夹爪上。
5. 两个标记尽量同高、同深度、同平面，并确保相机在整个开合范围内能同时看见它们。

“实际夹爪开口”统一指两个夹持内表面之间、沿夹爪开合方向的距离。

## 启动

在项目目录执行：

```powershell
python -m pip install -r requirements_visualizer.txt
.\run_experiment_monitor_windows.ps1
```

打开主窗口的 `ArUco Gripper` 页签，然后点击主窗口顶部的 `Start Monitor`。

## 最小/最大两点标定

推荐使用卡尺实测，不要只填写说明书标称行程。

1. 确认界面当前状态为“正在逐帧测量”，并且检测 ID 同时包含 `0`、`1`。
2. 把夹爪移动到最小开口。
3. 在“最小开口”中填写卡尺测得的实际距离；完全接触时可以填 `0 mm`。
4. 点击最小开口行的“记录当前”。软件会记录该帧的原始标记中心距离。
5. 把夹爪移动到最大开口。
6. 填写最大实际开口，点击最大开口行的“记录当前”。
7. 点击“计算/更新两点标定”。
8. 点击“保存并应用”。监控服务会自动重启并使用新标定。

四个标定数据都可以在 UI 中直接填写或修改，并会保存到 JSON：

```json
"distance_calibration": {
  "minimum": {
    "raw_marker_center_m": 0.04125,
    "actual_gap_m": 0.0
  },
  "maximum": {
    "raw_marker_center_m": 0.12180,
    "actual_gap_m": 0.0800
  }
}
```

程序自动计算：

```text
scale  = (实际最大开口 - 实际最小开口)
         / (原始最大距离 - 原始最小距离)

offset = 实际最小开口 - scale × 原始最小距离

每帧实际开口 = scale × 当前原始距离 + offset
```

配置文件同时保存计算结果 `distance_scale` 和 `distance_offset_m`，运行时无需重复标定。标记位置、打印尺寸或相机安装方式改变后，应重新做两点标定。

## 每帧处理逻辑

1. 从 APV2 视频帧读取图像和该帧相机内参。
2. 检测配置的两个 ArUco ID。
3. 使用 `16 mm` 黑色外边长和 `SOLVEPNP_IPPE_SQUARE` 求两个标记中心的相机坐标。
4. 计算两个 3D 中心的欧氏距离 `raw_marker_center_m`。
5. 使用两点标定得到真实夹爪开口。
6. 可选使用 EMA 平滑；`alpha=1.0` 表示不平滑。
7. 通过 UDP 5570 发送 AGP1 JSON。

只有同一帧同时检测到两个标记时，状态才是 `tracking_gripper_distance`。

## UDP 输出

示例：

```json
{
  "protocol": "AGP1",
  "frame_id": 125,
  "capture_time": 1784300000.123456,
  "status": "tracking_gripper_distance",
  "detected_ids": [0, 1],
  "gripper_distance": {
    "marker_ids": [0, 1],
    "raw_marker_center_m": 0.08153,
    "calibrated_m": 0.04002,
    "filtered_m": 0.04001,
    "calibrated_mm": 40.02,
    "filtered_mm": 40.01,
    "scale": 0.99317,
    "offset_m": -0.04095,
    "calibration_complete": true,
    "calibrated_range_mm": [0.0, 80.0]
  }
}
```

推荐下游使用 `filtered_mm`；不需要平滑时它与 `calibrated_mm` 相同。下游必须同时确认状态为 `tracking_gripper_distance` 且 `calibration_complete=true`。标定前仍会提供 `raw_marker_center_m`，用于在 UI 中记录最小/最大原始点，但不能把它当成真实夹爪开口。

接收和安全检查示例：

```powershell
python aruco_robot_pose_receiver.py --bind 127.0.0.1 --port 5570 `
  --min-gap-mm 0 --max-gap-mm 80 --max-step-mm 10
```

该示例会检查协议、状态、机械范围和相邻帧跳变量，再调用 `on_valid_distance()`。文件名为兼容旧入口而保留，但代码只接收夹爪距离，不再接收机器人位姿。

## 常见状态

- `tracking_gripper_distance`：当前帧距离有效。
- `insufficient_markers_for_distance`：只看到一个标记；调整相机、照明或遮挡。
- `no_markers`：两个配置 ID 都未检测到。
- `missing_intrinsics`：视频是旧 APV1 且未填写手工内参；优先使用 APV2。
- `processor_error`：处理异常，查看 UI 错误信息。

## 精度建议

- 打印后必须实测黑色边长；尺寸错误会直接形成比例误差。
- 标记应贴在平整刚性表面，不能随夹爪运动弯曲。
- 标定和实际测量时尽量保持两个标记都清晰、无遮挡。
- 最小点和最大点相距越大，两点标定越稳定。
- 需要降噪时可将 EMA 系数改为 `0.2–0.5`；需要最低延迟时保持 `1.0`。
