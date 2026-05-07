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
seq,t,x,y,z,qx,qy,qz,qw
t,x,y,z,qx,qy,qz,qw
x,y,z,qx,qy,qz,qw
```

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
- residual timing error after pairing
- sensor-to-ARKit scale
- sensor-to-ARKit rotation and translation
- a fixed quaternion orientation correction

Use `Apply adaptive calibration` to render and score the sensor stream after applying the estimated transform. The calibration needs real motion to be observable; move the phone/sensor together through forward/back, left/right, up/down, and rotation before trusting the estimate. Static data is not enough to infer axes or delay.
