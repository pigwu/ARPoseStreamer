# Architecture

## Overview

ARPoseStreamer has two main parts:

1. iPhone sender
2. Host receiver

The sender runs ARKit on iPhone and transmits compact pose packets over UDP. The receiver runs on macOS or Windows and prints or logs those packets.

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

## Host Side

Primary file:

- `udp_pose_receiver.py`

Responsibilities:

- bind a UDP socket
- decode binary or CSV packets
- show stream rate and packet-drop hints
- optionally log incoming pose to CSV

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
