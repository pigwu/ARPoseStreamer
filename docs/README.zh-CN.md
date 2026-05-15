# ARPoseStreamer 中文文档

> 对应英文主 README 的中文镜像版，帮助中文读者快速理解项目用途、安装方式、离线标定流程与使用边界。

语言 / Language: [English README](../README.md) | [简体中文](README.zh-CN.md)

## 项目概览

ARPoseStreamer 是一个轻量的 iPhone ARKit 位姿流采集与主机接收项目，主要能力包括：

- iPhone 端 ARKit 6-DoF 位姿流
- macOS / Windows 主机接收与可视化
- CSV 日志导出与离线分析
- 机器人末端与 ARKit 相机的 hand-eye 标定
- 时间同步、尺度估计、会话级 world 对齐

适合这些场景：

- 机器人与 ARKit 联合实验
- 远程操作 / teleoperation
- 视觉惯性里程计实验
- iPhone pose 采集数据管线
- 离线标定与误差评估

## 快速导航

- [英文主 README](../README.md)
- [离线标定总览（中文）](CALIBRATION_OVERVIEW.zh-CN.md)
- [Offline Calibration Overview (EN)](CALIBRATION_OVERVIEW.en.md)
- [Setup Guide](SETUP.md)
- [Architecture](ARCHITECTURE.md)
- [Protocol](PROTOCOL.md)

## 核心功能

### 1. 实时位姿流

ARPoseStreamer 在 iPhone 上运行 ARKit world tracking，把相机位姿编码成紧凑 UDP 包发送给主机。

每个包包含：

- 序列号
- 发送时间戳
- 平移 `x, y, z`
- 四元数 `qx, qy, qz, qw`

默认约定：

- 右手系
- `Z-up`

### 2. 主机接收与可视化

主机侧支持：

- 命令行 UDP 接收器
- 实时 3D 轨迹可视化
- CSV 日志记录
- 简单丢包与帧率监控

### 3. 离线标定与验证

现在仓库包含一套完整的离线标定流程，用于对齐：

- ARKit 相机位姿
- 机器人末端位姿

主要输出：

- `time_shift`
- `initial_scale_factor`
- `scale_factor`
- `T_cam2gripper`
- `T_base_world`
- reconstruction / relative / staged / cross-validation 误差

详细说明请阅读：

- [离线标定总览（中文）](CALIBRATION_OVERVIEW.zh-CN.md)

## 快速开始

### 依赖安装

```bash
pip install -r requirements_visualizer.txt
```

当前离线标定需要的关键 Python 依赖包括：

- `numpy`
- `scipy`
- `opencv-python-headless`
- `matplotlib`
- `PyQt6`
- `pyqtgraph`

### 实时可视化

macOS:

```bash
python3 udp_pose_visualizer.py
```

Windows:

```powershell
py udp_pose_visualizer.py
```

### 命令行接收器

macOS:

```bash
python3 udp_pose_receiver.py --host 0.0.0.0 --port 5555 --encoding binary
```

Windows:

```powershell
py udp_pose_receiver.py --host 0.0.0.0 --port 5555 --encoding binary
```

### 离线标定命令

```powershell
python pose_tracking_validator.py --mode offline --arkit-csv "uploads\20260511-225434\pose_csv__pose.csv" --sensor-csv "uploads\end_effector_pose_log (1).csv" --output-dir offline_calibration_output
```

可选增强参数：

- `--cross-validate-scale`
- `--cv-folds 5`
- `--skip-world-refinement`

示例：

```powershell
python pose_tracking_validator.py --mode offline --arkit-csv "uploads\20260511-225434\pose_csv__pose.csv" --sensor-csv "uploads\end_effector_pose_log (1).csv" --output-dir offline_calibration_output --cross-validate-scale --cv-folds 5
```

## 离线标定输出内容

每次运行会在 `offline_calibration_output/` 下创建一个带时间戳的独立子目录，包含：

- `offline_calibration_result.json`
- `velocity_time_alignment.png`
- `absolute_error_stages.png`
- `absolute_error_comparison.png`
- `axis_error_comparison.png`
- `trajectory_overlap_3d.png`
- `scale_cross_validation.png`（如果启用交叉验证）

## 这套标定能做什么

- 验证 ARKit 位姿与机器人真值的一致性
- 把 ARKit 相机位姿转换为机器人末端位姿
- 估计固定 hand-eye 外参 `T_cam2gripper`
- 估计会话级 `T_base_world`
- 量化尺度 overfitting 风险

## 这套标定不能自动保证什么

- 不能自动保证 `scale_factor` 就是外部真值物理尺度
- 不能只凭样本内 absolute error 就断言“ARKit 位姿是真实位姿”
- 不能在手机脱离机械臂独立采集后，自动把那段轨迹映射回机器人 base

更准确地说，当前 `scale_factor` 是：

- 由速度法提供 `initial_scale_factor`
- 再通过多帧几何一致性优化得到的“有效尺度”

## 交叉验证风险量化

项目已经支持 block-wise cross-validation，用来量化：

- 训练集误差
- 验证集误差
- 训练/验证 gap
- refined scale 相比初始 scale 的验证集收益

这一步是为了避免把“训练误差最小”误当成“真实参数”。

## 哪些量能复用，哪些不能

### 可长期复用

- `T_cam2gripper`

前提：手机装夹方式不变。

### 会话相关

- `T_base_world`
- `time_shift`
- `scale_factor`

只要 ARKit world 重置、会话切换、原点重设，这些量通常都需要重新估计。

## 如果手机脱离机械臂单独采集怎么办

如果手机离开机械臂去别处采集，那么它得到的是手机自身局部坐标系下的轨迹，不会自动知道机器人 base 在哪里。

当前这套标定只解决：

“手机固定在机械臂末端时，如何把 ARKit pose 转成机器人末端 pose”

它不能单独解决：

“手机离开机械臂后，在别处采的轨迹如何自动回到机器人 base 坐标”

如果要解决后者，需要额外的共同参考，例如：

- AprilTag / ArUco
- ARKit world map relocalization
- 固定停靠位
- Mocap / UWB / 外部定位系统

## 术语建议

为了避免表达过度，可以使用这些术语：

- “有效尺度估计” 代替 “真实物理尺度”
- “重建误差” 代替 “独立验证误差”
- “手眼一致性误差” 表示 `AX = XB` 残差
- “会话级 world 对齐” 表示 `T_base_world`

## 推荐阅读顺序

如果你第一次接触这个项目，建议按下面顺序阅读：

1. [英文主 README](../README.md)
2. [离线标定总览（中文）](CALIBRATION_OVERVIEW.zh-CN.md)
3. [Setup Guide](SETUP.md)
4. [Architecture](ARCHITECTURE.md)

## License

MIT
