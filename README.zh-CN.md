# ARPoseStreamer

<div align="center">

![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)
![Platform: iOS](https://img.shields.io/badge/platform-iOS-blue.svg)
![Receiver: macOS%20%7C%20Windows](https://img.shields.io/badge/receiver-macOS%20%7C%20Windows-lightgrey.svg)

**一个轻量的 iPhone ARKit 位姿采集、录制、上传与电脑端接收项目。**

**语言 / Language**: [English](README.md) | [简体中文](README.zh-CN.md)

![ARPoseStreamer Hero](docs/assets/hero-banner.svg)

</div>

---

## 项目简介

ARPoseStreamer 在 iPhone 上运行 ARKit world tracking，把相机 6-DoF 位姿通过 UDP 发送到 macOS 或 Windows 主机，也可以在手机本地录制视频、pose CSV 和 manifest，之后再通过 HTTP 上传。

它适合这些场景：

- iPhone ARKit 位姿采集
- 机器人、遥操作和 UMI/VIO 风格实验
- 电脑端实时轨迹可视化
- 本地录制后离线分析
- 检查 UDP 实时传输质量

## 主要功能

- iPhone 端 ARKit 6-DoF 位姿流
- 默认 UDP 二进制包：`sequence, sender_time, x, y, z, qx, qy, qz, qw`
- 电脑端命令行接收器和 3D 可视化器
- iPhone 本地 MP4 录制、pose CSV 和 manifest 导出
- HTTP 上传历史记录到电脑
- 可配置接收端 IP、UDP 端口和上传端口
- capture history 重命名与重新上传
- XcodeGen 工程配置

## 快速开始

安装 Python 依赖：

```bash
pip install -r requirements_visualizer.txt
```

启动 3D 可视化器：

Windows:

```powershell
.\run_visualizer_windows.ps1
```

macOS:

```bash
./run_visualizer_mac.sh
```

只启动命令行 UDP 接收器：

```bash
python udp_pose_receiver.py --host 0.0.0.0 --port 5555 --encoding binary
```

在 iPhone App 中：

1. 打开设置。
2. 把 `Host IP` 填为电脑的局域网 IP。
3. 把 `Port` 填为 `5555`。
4. 回到主界面，打开菜单并点击 `Start Streaming`。
5. 如需本地采集，点击主界面底部的 `Start Recording`。

## 相关独立项目

这些工具已经拆成独立仓库，但和本项目可以互相配合：

- [iPhone UDP Packet Loss Monitor](https://github.com/pigwu/iPhoneUDPPacketLossMonitor)：独立电脑端 UDP 丢包率监测界面，用于查看丢包率、FPS、jitter、延迟、重复包和乱序包。
- [iPhone Trajectory Validator](https://github.com/pigwu/iPhoneTrajectoryValidator)：独立离线轨迹验证工具，用参考机器人日志验证 iPhone ARKit 轨迹质量，默认固定 `scale = 1.0`，不做尺度拟合。

推荐分工：

- 需要 iPhone App、实时发送、录制、上传和 3D 可视化时，用 ARPoseStreamer。
- 只想检测手机到电脑的 UDP 网络质量时，用 iPhone UDP Packet Loss Monitor。
- 只想用上传后的 CSV 验证轨迹质量时，用 iPhone Trajectory Validator。

## 数据与包格式

默认二进制 UDP 包为 little-endian：

```text
UInt32 sequence
Float64 sender_time
Float32 x, y, z
Float32 qx, qy, qz, qw
```

总长度为 40 字节，对应 Python：

```python
struct.Struct("<Id7f")
```

CSV 调试格式：

```text
sequence,sender_time,x,y,z,qx,qy,qz,qw
```

## 离线验证说明

离线轨迹验证已经拆到独立项目：

- [iPhoneTrajectoryValidator](https://github.com/pigwu/iPhoneTrajectoryValidator)

当前建议的验证逻辑是：上传 iPhone ARKit CSV 和参考机器人末端 CSV，固定 `scale = 1.0`，只用机器人作为验证参考，不把机器人坐标系变换当成手机运行时必须复用的配置。

## 项目结构

- `ARPoseUDPSender.swift`：ARKit session、UDP 发送和录制状态协调
- `ContentView.swift`：iPhone 主界面
- `AppSettingsView.swift`：接收端和上传设置
- `CaptureHistoryView.swift`：历史记录、重命名和上传
- `CaptureUploadService.swift`：手机端 HTTP 上传客户端
- `udp_pose_receiver.py`：命令行 UDP 接收器
- `udp_pose_visualizer.py`：电脑端 3D 实时轨迹可视化器和上传服务
- `capture_upload_server.py`：独立 HTTP 上传服务器
- `PoseDataSessionRecorder.swift`：pose CSV 与 manifest 录制
- `ARSessionVideoRecorder.swift`：MP4 视频录制
- `project.yml`：XcodeGen 配置

## 安装 iPhone App

请参考：

- [INSTALL_IPHONE_APP.md](INSTALL_IPHONE_APP.md)

简要流程：

1. 在 Mac 上安装 Xcode。
2. 安装 XcodeGen。
3. 运行 `xcodegen generate`。
4. 打开生成的 Xcode 工程。
5. 选择 Apple signing team。
6. Build 到连接的 iPhone。

## 许可证

MIT
