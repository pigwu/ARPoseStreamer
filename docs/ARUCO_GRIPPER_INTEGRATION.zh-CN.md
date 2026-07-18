# ArUco 夹爪逐帧距离测量

## 当前策略

当前测距方法已与 UMI-FT 的核心几何策略对齐：

1. 使用 `DICT_4X4_50`、ID 0/1、黑色外边长 16 mm。
2. 使用相机内参和标记尺寸估计每个标记的相机坐标 `tvec=(x,y,z)`。
3. 原始夹爪宽度取两个标记在相机 X 轴上的间距：

```text
raw_width = abs(x_id1 - x_id0)
```

4. 默认只接受深度位于 `72±8 mm` 的两个标记。
5. 使用连续开合至少 5 个完整周期的稳健端点统计值做两点标定。
6. 每帧通过 UI、UDP 和可选 CSV 输出实际夹爪开口。

与 UMI-FT 相比，本实现保留两个更安全的约束：必须在同一帧同时看到两个标记；实际最小/最大开口由卡尺测量，因此可同时校正比例和偏移。

本功能不使用机械臂型号、TCP、`marker→tool`、`T_base_world` 或 ARKit 位姿融合。

## 相机内参

APV2 每帧携带 ARKit 提供的：

```text
K = [ fx  0  cx ]
    [ 0  fy  cy ]
    [ 0   0   1 ]
```

这些参数用于把图像像素与相机射线对应起来，正常情况下无需手工填写。只有使用旧 APV1 时才需要在高级设置中填写回退内参。

## 标记安装

1. 将 PDF 按 100% 原始尺寸打印，不要选择“适合页面”。
2. 实测每个标记的黑色外边长，应为 `16.000 mm`。
3. ID 0、ID 1 分别刚性固定在两个活动夹爪上。
4. 两个标记尽量同高、同深度、同平面，并让相机 X 轴与夹爪开合方向一致。
5. 确保整个开合范围内相机可以同时看到两个标记。

“实际夹爪开口”指两个夹持内表面之间、沿开合方向的距离。

## 启动

```powershell
python -m pip install -r requirements_visualizer.txt
.\run_experiment_monitor_windows.ps1
```

点击主窗口顶部 `Start Monitor`，再打开 `ArUco Gripper` 页签。

## 连续开合标定

1. 确认状态为 `tracking_gripper_distance`，ID 同时包含 0、1。
2. 确认标记深度接近默认的 `72±8 mm`；若安装结构不同，修改“标记标称深度”和“允许深度偏差”。
3. 用卡尺填写最小实际开口；夹持面完全接触时可填 `0 mm`。
4. 用卡尺填写最大实际开口。
5. 点击“开始采集”。
6. 连续完成至少 5 次：全闭 → 全开 → 全闭。
7. UI 会显示有效帧数、检测到的完整周期数和稳健端点。
8. 达到要求后点击“结束采集并计算”。
9. 点击“保存并应用”。

周期不足时，UI 不会覆盖已有标定。算法对每次到达全闭/全开时的极值做统计，再取多个周期极值的中位数，避免单帧噪声直接决定端点。

计算公式：

```text
scale  = (实际最大开口 - 实际最小开口)
         / (原始最大 X 宽度 - 原始最小 X 宽度)

offset = 实际最小开口 - scale × 原始最小 X 宽度

每帧实际开口 = scale × 当前原始 X 宽度 + offset
```

## 保存的数据

配置文件保存测量策略、深度门限、周期要求和两个端点：

```json
{
  "distance_measurement_mode": "camera_x",
  "nominal_marker_depth_m": 0.072,
  "marker_depth_tolerance_m": 0.008,
  "distance_scale": 1.0,
  "distance_offset_m": -0.0412,
  "distance_calibration": {
    "minimum_cycles": 5,
    "minimum": {
      "raw_marker_x_distance_m": 0.0412,
      "actual_gap_m": 0.0
    },
    "maximum": {
      "raw_marker_x_distance_m": 0.1212,
      "actual_gap_m": 0.0800
    }
  }
}
```

标记位置、打印尺寸、相机镜头或安装结构改变后，应重新标定。

## 每帧 UDP 输出

默认目标为 UDP `127.0.0.1:5570`，协议为 AGP1 JSON：

```json
{
  "protocol": "AGP1",
  "frame_id": 125,
  "status": "tracking_gripper_distance",
  "measurement": {
    "mode": "camera_x",
    "nominal_marker_depth_m": 0.072,
    "marker_depth_tolerance_m": 0.008,
    "marker_depth_m": {"0": 0.0718, "1": 0.0721}
  },
  "gripper_distance": {
    "marker_ids": [0, 1],
    "measurement_mode": "camera_x",
    "raw_marker_x_distance_m": 0.0814,
    "marker_center_distance_3d_m": 0.0815,
    "calibrated_mm": 40.2,
    "filtered_mm": 40.1,
    "calibration_complete": true,
    "calibrated_range_mm": [0.0, 80.0]
  }
}
```

- 下游推荐使用 `filtered_mm`。
- `marker_center_distance_3d_m` 只用于诊断，不参与夹爪宽度计算。
- 下游必须同时确认 `status=tracking_gripper_distance` 和 `calibration_complete=true`。

接收示例：

```powershell
python aruco_robot_pose_receiver.py --bind 127.0.0.1 --port 5570 `
  --min-gap-mm 0 --max-gap-mm 80 --max-step-mm 10
```

## 状态说明

- `tracking_gripper_distance`：当前帧两个标记和深度均有效。
- `insufficient_markers_for_distance`：当前帧缺少 ID 0 或 ID 1。
- `marker_depth_out_of_range`：至少一个标记不在配置的深度范围内。
- `no_markers`：两个配置 ID 都未检测到。
- `missing_intrinsics`：旧 APV1 没有提供内参；应更新手机 App 或填写回退内参。
- `processor_error`：处理异常，查看 UI 错误信息。

## 精度建议

- 使用 APV2 的逐帧 ARKit 相机内参。
- 相机必须刚性安装，并尽量让夹爪运动方向平行于相机 X 轴。
- 若标记实际深度不是 72 mm，应修改标称深度，而不是盲目扩大容差。
- 标定时在端点稍作停留，并完成 5–8 个完整周期。
- 需要最低延迟时保持 EMA `alpha=1.0`；需要降噪时可使用 `0.2–0.5`。
