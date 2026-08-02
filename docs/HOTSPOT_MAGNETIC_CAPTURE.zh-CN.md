# 手机热点磁传感器采集

本模式用于学校 Wi-Fi 需要网页或企业认证、传感器板无法接入，并且采集现场不一定有电脑的情况。

## 数据链路

```text
右侧板 --ASKN/UDP 5557 --\
                           > iPhone APP --> 手机本地 Capture
左侧板 --ASKN/UDP 5562 --/       |
ARKit 位姿 ----------------------^ +--> 电脑在线时发送 APM2
```

电脑不是采集的必要条件。无论电脑是否在线，APP 都会把位姿、磁数据和可选视频保存到同一个会话目录。

两块磁传感器在试验阶段都是可选输入：某一侧没有连接不会影响另一侧，也不会影响 ARKit 预览、位姿/视频录制、电脑端位姿传输、历史记录和补传。两侧都没有数据时，APM2 包的 `magnetic_count` 为 0。APP 在各侧收到首个有效数据后，分别创建对应的 CSV。

## 默认端口

| 方向 | 端口 | 用途 |
|---|---:|---|
| 右侧板到手机 | UDP 5557 | 96 字节 ASKN 磁数据 |
| 左侧板到手机 | UDP 5562 | 96 字节 ASKN 磁数据 |
| 手机到电脑 | UDP 5558 | APM2 位姿和双侧磁数据组合包 |
| 电脑到手机 | UDP 5559 | `PC_HELLO` 注册心跳 |
| 手机到电脑 | UDP 5560 | 可选低延迟视频 |
| 手机到电脑 | TCP 8000 | 历史文件 HTTP 上传 |

## 使用步骤

1. 在 iPhone 设置中打开“个人热点”和“允许其他人加入”。
2. ESP32 等只支持 2.4 GHz 的板卡应打开“最大兼容性”。
3. iPhone 名称会作为热点 SSID，建议使用简单英文和数字；密码也建议只使用 ASCII 字符。
4. 两块板都保存热点 SSID 和密码，启动后自动重连。
5. 两块板都从 DHCP 获取 Gateway 地址。右侧板向 `Gateway:5557`、左侧板向 `Gateway:5562` 发送 ASKN 数据。不要把 `172.20.10.1` 写死在固件中。
6. 打开 APP。默认自动启动两侧磁数据监听；设置页可以分别查看收包率、丢包、板卡地址、序号和 5 个芯片的实时数值。
7. 点击 `Start Recording`。APP 会在本地生成：

```text
Captures/<session>/
  pose.csv
  magnetic_right.csv      # 右侧收到数据时存在
  magnetic_left.csv       # 左侧收到数据时存在
  video.mp4               # 录制视频时存在
  capture_manifest.json
```

第三方 APP 无法使用公开 iOS API 自动开启个人热点，因此采集前必须手动打开热点。本链路也必须在目标 iPhone 真机上验证，模拟器不能验证个人热点接口。

## 电脑在场时

电脑连接同一个 iPhone 热点，然后运行：

```powershell
py pose_magnetic_receiver.py --phone-ip 172.20.10.1
```

macOS/Linux：

```bash
python3 pose_magnetic_receiver.py --phone-ip 172.20.10.1
```

电脑每两秒向手机 UDP 5559 发送一次 `PC_HELLO`。APP 从数据包来源自动识别电脑地址，并把带左右标识的 APM2 组合数据发到电脑 UDP 5558。电脑端仍兼容旧 APM1，并把旧单板样本视为右侧数据。电脑断开时，APP 只停止实时转发，本地录制不会中断。

热点网关通常是 `172.20.10.1`，但电脑端应以实际默认网关为准；板卡固件必须使用 DHCP 下发的 Gateway。

## 电脑不在时及补传

结束采集后，记录保留在 APP 的 `Past Records` 页面。电脑之后连接手机热点或同一局域网时：

```powershell
py capture_upload_server.py --host 0.0.0.0 --port 8000
```

在 APP 中设置电脑 IP，再选择 `Past Records -> Upload Data`，即可上传 `pose.csv`、存在的 `magnetic_right.csv` / `magnetic_left.csv` 和 manifest；视频使用单独的 `Upload Video`。

## 联调工具

没有真实板卡时，可以从另一台连接手机热点的电脑模拟发送磁数据：

```powershell
py anyskin_hotspot_sender.py --host 172.20.10.1 --port 5557 --rate 100
```

测试左侧时把端口改为 `5562`。两侧联调需要同时启动两个发送进程。

协议和端口详细定义见 [PROTOCOL.md](PROTOCOL.md)。
