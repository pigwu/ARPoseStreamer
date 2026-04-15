# ARPoseStreamer

ARPoseStreamer is a lightweight ARKit-based iPhone app for streaming relative camera pose over UDP to a Mac or Windows receiver.

It is designed for robotics, teleoperation, and computer vision experiments where you want a simple iPhone-to-host pose stream without SceneKit or RealityKit rendering code.

## Features

- ARKit world tracking
- Relative pose with resettable origin
- 60 Hz oriented streaming pipeline
- UDP transport to a configurable host IP on port `5555`
- Cross-platform receiver on macOS and Windows
- Compact pose packets with:
  - sequence number
  - sender timestamp
  - `x, y, z, qx, qy, qz, qw`
- Default output frame:
  - `Z-up`
  - right-handed

## Repository Layout

- `ARPoseUDPSender.swift`: ARKit + UDP sender
- `ARPositionApp.swift`: SwiftUI app entry
- `ContentView.swift`: iPhone UI
- `PositionViewModel.swift`: sender/UI wiring
- `Info.plist`: camera and local network permission strings
- `project.yml`: XcodeGen spec for generating the Xcode project
- `udp_pose_receiver.py`: Python UDP receiver for macOS/Windows
- `run_receiver_mac.sh`: helper launcher for macOS
- `run_receiver_windows.ps1`: helper launcher for Windows
- `INSTALL_IPHONE_APP.md`: iPhone installation guide

## Packet Format

Default packet encoding is binary UDP, little-endian.

Binary layout:

1. `sequence` as `UInt32`
2. `sender_time` as `Float64`
3. `x` as `Float32`
4. `y` as `Float32`
5. `z` as `Float32`
6. `qx` as `Float32`
7. `qy` as `Float32`
8. `qz` as `Float32`
9. `qw` as `Float32`

Total packet size: `40` bytes.

CSV mode is also supported for debugging:

```text
sequence,sender_time,x,y,z,qx,qy,qz,qw
```

## Quick Start

### 1. Run The Receiver

On macOS:

```bash
python3 udp_pose_receiver.py --host 0.0.0.0 --port 5555 --encoding binary
```

or:

```bash
sh run_receiver_mac.sh
```

On Windows:

```powershell
py udp_pose_receiver.py --host 0.0.0.0 --port 5555 --encoding binary
```

or:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_receiver_windows.ps1
```

Optional logging:

```bash
python3 udp_pose_receiver.py --encoding binary --csv-log logs/pose.csv
```

### 2. Find The Receiver IP

On macOS:

```bash
ipconfig getifaddr en0
```

On Windows:

```powershell
ipconfig
```

Use the IPv4 address of the machine running `udp_pose_receiver.py`.

### 3. Install The iPhone App

Follow:

- `INSTALL_IPHONE_APP.md`

### 4. Start Streaming

On the iPhone:

1. Open the app
2. Enter the receiver IP
3. Tap `Start Streaming`
4. Allow:
   - camera access
   - local network access

### 5. Verify

The receiver should print:

- increasing sequence numbers
- near-zero drop count on a stable LAN
- roughly 60 FPS receive rate
- live pose values

The printed `approx_lat` value is only meaningful when the iPhone and receiver clocks are reasonably synchronized.

## iPhone Installation Notes

This repository contains source code, not a universally installable iPhone binary.

To install on an iPhone, users must build and sign the app locally with Xcode using their own Apple account, or distribute it through TestFlight / App Store workflows.

For local installation:

- install Xcode on a Mac
- generate the Xcode project with XcodeGen
- sign with your own Apple team
- build and run on a connected iPhone

## Notes For Robotics / UMI-Style Use

- The public Stanford `iPhoneVIO` example sends a full `4x4` transform over Socket.IO.
- This project is intentionally lighter and sends only the pose state required downstream.
- If your downstream stack expects quaternion order `wxyz`, reorder from `xyzw`.
- If your downstream stack expects ARKit's original `Y-up` frame, update the sender configuration accordingly.

## Limitations

- iPhone installation requires a Mac and Xcode
- Personal-team builds may expire after a short period
- LAN UDP can drop packets, so the sequence field is included for drop detection

## License

MIT
