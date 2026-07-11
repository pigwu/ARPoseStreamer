# Architecture

## Overview

ARPoseStreamer has three cooperating parts:

1. optional Wi-Fi magnetic sensor board
2. iPhone capture gateway
3. optional host receiver

The iPhone always remains the capture authority. It runs ARKit, receives optional magnetic samples from a board on the phone hotspot, records locally, and forwards combined data only while a computer is registered. The computer is not required during capture.

## Data Flow

```text
ARKit camera pose
    ->
relative transform
    ->
coordinate conversion
    ->
packet encoding
    ->
UDP send
    ->
Python receiver
    ->
terminal / CSV / downstream pipeline
```

Hotspot magnetic flow:

```text
optional board -- ASKN UDP 5557 --> iPhone gateway
ARKit pose -----------------------> timestamp-preserving mux
                                      |-> local pose/magnetic/video capture
                                      `-> APM1 UDP 5558 when a PC is registered
```

## iPhone Side

Primary files:

- `ARPoseUDPSender.swift`
- `PositionViewModel.swift`
- `ContentView.swift`
- `ARPositionApp.swift`

Responsibilities:

- configure `ARSession`
- run AR world tracking
- keep a resettable relative origin
- convert pose to the chosen coordinate convention
- package pose into a compact UDP payload
- stream to the configured host IP on port `5555`
- optionally record camera video to a local MP4 file
- store pose CSV and capture manifest for offline export
- register each capture in a persistent local history library
- receive five-chip ASKN magnetic samples from the hotspot DHCP gateway path
- keep magnetic input optional so pose/video operation never waits for a board
- multiplex pose and zero or more magnetic samples into APM1 packets
- discover an optional computer through `PC_HELLO` heartbeats on UDP `5559`

## Host Side

Primary file:

- `udp_pose_receiver.py`
- `capture_upload_server.py`
- `pose_magnetic_receiver.py`

Responsibilities:

- bind a UDP socket
- decode binary or CSV packets
- show stream rate and packet-drop hints
- optionally log incoming pose to CSV
- receive uploaded capture files over HTTP
- register with the phone and persist combined pose/magnetic live data

## Design Goals

- small codebase
- no rendering dependency
- low setup friction
- easy adaptation for robotics and CV labs
- easy replacement of the Python receiver with another downstream system

## Suggested Extensions

- ROS2 bridge
- ZMQ bridge
- shared memory writer
- 4x4 transform reconstruction on the receiver
- optional smoothing or calibration transforms
