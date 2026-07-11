# Setup Guide

## Goal

Get the iPhone app streaming ARKit pose packets to a receiver on macOS or Windows.

## macOS Receiver

Run:

```bash
python3 udp_pose_receiver.py --host 0.0.0.0 --port 5555 --encoding binary
```

Optional real upload server:

```bash
python3 capture_upload_server.py --host 0.0.0.0 --port 8000
```

Find your IP:

```bash
ipconfig getifaddr en0
```

## Windows Receiver

Run:

```powershell
py udp_pose_receiver.py --host 0.0.0.0 --port 5555 --encoding binary
```

Optional real upload server:

```powershell
py capture_upload_server.py --host 0.0.0.0 --port 8000
```

Find your IP:

```powershell
ipconfig
```

## iPhone App Installation

See the full guide:

- `../INSTALL_IPHONE_APP.md`

Short version:

1. install Xcode
2. install XcodeGen
3. run `xcodegen generate`
4. open the generated Xcode project
5. select your Apple signing team
6. connect the iPhone
7. build and run from Xcode

## iPhone App Usage

1. open the app
2. open the side menu
3. open the settings sheet
4. enter the host IP and choose the receiver OS
5. start streaming or video recording from the side menu
6. allow camera access
7. allow local network access

Recorded videos are saved inside the app Documents directory and can be exported later through Files or Finder file sharing.
Pose CSV and capture manifest files are also saved locally, so offline sessions can be uploaded later even without a live host connection.
Each capture segment is stored as a separate historical record and can be renamed later in the app.
For actual uploads, open `Past Records` and use the upload buttons after you start the host upload server.

## Phone Hotspot Magnetic Capture

This mode does not require the school Wi-Fi or a computer during capture.

1. On the iPhone, enable Personal Hotspot and `Allow Others to Join`.
2. For 2.4 GHz boards such as many ESP32 variants, enable `Maximize Compatibility`.
3. Use an ASCII-only iPhone name and hotspot password for the most reliable embedded Wi-Fi compatibility.
4. Configure the board with the hotspot SSID/password and power it from the phone USB-C port.
5. The board reads the DHCP gateway and sends 96-byte ASKN UDP packets to gateway port `5557`.
6. Open the app. The magnetic listener starts automatically by default and shows receive rate, loss, sequence, and five-chip values.
7. Tap `Start Recording`. Pose, magnetic data, and optional video are saved in one local capture directory.

The app cannot turn Personal Hotspot on through a public iOS API. Enable it manually before powering the board. The board must use the DHCP gateway address instead of assuming the phone is always `172.20.10.1`.

The magnetic board is optional during experiments. With no board connected,
ARKit preview, pose/video recording, legacy pose UDP, APM1 pose packets with
`magnetic_count = 0`, history, and later upload all continue to work. The app
creates `magnetic.csv` only after the first valid magnetic sample arrives.

When a computer is available, connect it to the same hotspot and run:

```bash
python3 pose_magnetic_receiver.py --phone-ip 172.20.10.1
```

On Windows:

```powershell
py pose_magnetic_receiver.py --phone-ip 172.20.10.1
```

The computer sends `PC_HELLO` heartbeats to the app and receives combined APM1 packets on UDP `5558`. If the computer disconnects, local phone recording continues. Start `capture_upload_server.py` later and use `Past Records -> Upload Data` to transfer `pose.csv`, `magnetic.csv`, and the capture manifest.

Personal Hotspot routing must be validated on the target iPhone and iOS version; the simulator cannot test this path.

## Validation Checklist

- receiver is listening on port `5555`
- iPhone and host are on the same LAN
- sequence numbers increase
- receive rate is near 60 Hz
- pose values change when the phone moves

## Wired Sensor Validation

The app has an optional wired sensor path for iOS-supported ExternalAccessory hardware.

Expected setup:

- the accessory supports iOS ExternalAccessory communication
- `Info.plist` contains the accessory protocol string under `UISupportedExternalAccessoryProtocols`
- the app settings use the same accessory protocol string
- the sensor writes one UTF-8 pose sample per line

Accepted serial line formats:

```text
AP2,2,source,seq,t,x,y,z,qx,qy,qz,qw,checksum
seq,t,x,y,z,qx,qy,qz,qw
t,x,y,z,qx,qy,qz,qw
x,y,z,qx,qy,qz,qw
```

The Settings screen can list visible iOS ExternalAccessory devices and their `protocolStrings`. Use that list to copy the real accessory protocol into the app setting and `Info.plist`.

Start the validator on the host:

```bash
python pose_tracking_validator.py --host 0.0.0.0 --arkit-port 5555 --sensor-port 5556
```

Then on the iPhone:

1. open Settings and confirm the host IP, ARKit UDP port, sensor UDP port, and accessory protocol
2. start normal ARKit streaming
3. start `Wired Sensor` from the side menu

The validator renders ARKit in cyan and the wired sensor in amber from an external camera view. It also reports position error, quaternion angle error, and pairing time delta for the closest timestamped samples.

The validator also includes adaptive calibration. After both streams receive enough motion, it estimates:

- sensor-to-ARKit time offset
- sensor clock drift when sensor timestamps are available
- residual timing error after pairing
- sensor-to-ARKit scale
- sensor-to-ARKit rotation and translation
- a fixed quaternion orientation correction
- calibration quality, inlier ratio, and motion coverage

Use `Apply adaptive calibration` to render and score the sensor stream after applying the estimated transform. The calibration needs real motion to be observable; move the phone/sensor together through forward/back, left/right, up/down, and rotation before trusting the estimate. Static data is not enough to infer axes or delay.

The validator can also run offline from CSV logs:

```bash
python pose_tracking_validator.py --arkit-csv pose.csv --sensor-csv sensor_pose.csv
```

Use `Save Calibration` and `Load Calibration` in the validator to reuse a stable sensor-to-ARKit transform for the same hardware mounting.
