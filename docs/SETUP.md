# Setup Guide

## Goal

Get the iPhone app streaming ARKit pose packets to a receiver on macOS or Windows.

## macOS Receiver

Run:

```bash
python3 udp_pose_receiver.py --host 0.0.0.0 --port 5555 --encoding binary
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
2. enter the host IP
3. tap `Start Streaming`
4. optionally tap `Start Video Recording`
5. allow camera access
6. allow local network access

Recorded videos are saved inside the app Documents directory and can be exported later through Files or Finder file sharing.

## Validation Checklist

- receiver is listening on port `5555`
- iPhone and host are on the same LAN
- sequence numbers increase
- receive rate is near 60 Hz
- pose values change when the phone moves
