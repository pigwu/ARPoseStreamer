# AnySkin UDP Monitor

`anyskin_udp_monitor.py` 是一个电脑端 Wi-Fi/UDP 实时接收界面，用来接收 5 个磁传感器芯片的数据、显示实时曲线，并同步保存 CSV。

## 运行

先安装桌面工具依赖：

```powershell
pip install -r requirements_visualizer.txt
```

启动 UI：

```powershell
python anyskin_udp_monitor.py --port 5555
```

也可以打开后自动开始监听：

```powershell
python anyskin_udp_monitor.py --port 5555 --auto-start
```

默认绑定 `0.0.0.0:5555`。界面顶部会显示电脑当前 IP，Wi-Fi 板发送 UDP 时目标 IP 填这个电脑 IP，目标端口填 `5555`。

## 数据协议

每个 UDP 包必须是小端二进制格式：

```text
uint32 magic        0x41534B4E, "ASKN"
uint32 seq
uint64 mcu_time_us
float32 sensor[5][4]
```

总包长是 96 字节。5 个芯片按 `S0` 到 `S4` 显示，每个芯片字段为：

```text
t, x, y, z
```

## 界面含义

- `Status`：当前是否在监听、是否已经收到有效包。
- `Source`：最新 UDP 包的发送端 IP 和端口。
- `Receive Rate`：最近 5 秒接收频率，以及启动后的整体频率。
- `Loss`：根据 `seq` 计算的丢包率，同时统计重复包和乱序包。
- `Sequence`：最新 `seq` 和 `mcu_time_us`。
- `Latest 5-chip values`：最新一帧 5 个芯片的 `t/x/y/z`。
- `Realtime plots`：上图显示选中芯片的 `t/x/y/z`，下图显示 5 个芯片的 `sqrt(x^2+y^2+z^2)`。

## CSV 日志

勾选 `Save CSV` 时，日志会保存到界面中的 CSV folder。默认文件名类似：

```text
logs/anyskin_udp_log_20260625_153000.csv
```

CSV 字段：

```text
pc_receive_time,seq,mcu_time_us,
s0_t,s0_x,s0_y,s0_z,
s1_t,s1_x,s1_y,s1_z,
s2_t,s2_x,s2_y,s2_z,
s3_t,s3_x,s3_y,s3_z,
s4_t,s4_x,s4_y,s4_z
```

`pc_receive_time` 使用电脑接收 UDP 包时的 Unix 时间戳，适合后续离线对齐到电脑或手机时间轴。
