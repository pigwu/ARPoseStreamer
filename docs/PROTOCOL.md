# Protocol

## Default Transport

- protocol: UDP
- destination port: `5555`
- default encoding: binary
- byte order: little-endian

## Packet Fields

Default binary packet layout:

1. `sequence` as `UInt32`
2. `sender_time` as `Float64`
3. `x` as `Float32`
4. `y` as `Float32`
5. `z` as `Float32`
6. `qx` as `Float32`
7. `qy` as `Float32`
8. `qz` as `Float32`
9. `qw` as `Float32`

Total:

```text
40 bytes
```

## CSV Debug Format

```text
sequence,sender_time,x,y,z,qx,qy,qz,qw
```

## Semantics

- `sequence`: monotonically increasing packet counter
- `sender_time`: wall-clock time from the sender at packet creation
- `x, y, z`: relative position
- `qx, qy, qz, qw`: quaternion orientation

## Coordinate Convention

Default convention in this repository:

- right-handed
- `Z-up`

If you change the sender to ARKit-native output, update the documentation and the receiver expectations.

## Notes

- UDP may drop packets or reorder them
- the sequence field helps detect drops
- the sender timestamp can be used for rough latency estimation when clocks are reasonably aligned

## Phone-Hotspot Magnetic Sensor Input

The sensor board joins the iPhone Personal Hotspot and sends UDP datagrams to
the DHCP gateway address on port `5557`. The board must discover the gateway
from DHCP rather than hard-code `172.20.10.1`.

ASKN v1 input is exactly 96 bytes and uses little-endian values:

```text
uint32 magic = 0x41534B4E
uint32 sequence
uint64 mcu_time_us
float32 sensor[5][4]  // each chip is t, x, y, z
```

Because `magic` is encoded as a little-endian integer, its wire bytes are
`4E 4B 53 41`. They are not raw ASCII `ASKN`.

The app replies once per UDP flow with the optional UTF-8 acknowledgement:

```text
APP_ACK,1,5557\n
```

The app records every valid sample locally. Computer availability never gates
local recording.

## Computer Registration

A computer connected to the same phone hotspot registers by sending the
following UTF-8 datagram to phone UDP port `5559` every two seconds:

```text
PC_HELLO,1,combined_port,video_port\n
```

The app obtains the computer IP from the datagram source endpoint and leases it
for five seconds. The phone does not reply because the desktop tool uses the
same socket for APM1 receive traffic.

The integrated experiment monitor can also control the phone's unified
experiment recorder through the same UDP registration port. The command is:

```text
PC_RECORD,1,<request_id>,<START|STOP|STATUS>
```

The phone routes `START` and `STOP` through the same `PositionViewModel`
methods used by the App button. It replies to the source socket with:

```text
PC_RECORD_ACK,1,<request_id>,<action>,<OK|REJECTED>,<idle|recording|saving|busy>
```

The desktop must not assume a command succeeded before receiving this reply or
the existing HTTP `/experiment/control` event. `STATUS` is side-effect free and
is used to synchronize the desktop button after startup or reconnect.

After `STOP` hands the finished writers to background finalization, the phone
may acknowledge state `idle` immediately. A new `START` can then create an
independent recording context while the previous experiment finishes saving
and uploading; files and experiment identifiers remain isolated.

## Combined Pose and Magnetic Stream (APM1)

After registration, the app sends APM1 UDP datagrams to the advertised
`combined_port`, normally `5558`. All numeric fields are little-endian:

```text
char[4] magic = "APM1"
uint16 version = 1
uint16 flags                 // bit 0: pose block is valid
uint32 packet_sequence
uint8 session_uuid[16]
float64 phone_send_unix

uint32 pose_sequence
float64 pose_sender_unix
float64 pose_frame_monotonic
float32 position[3]
float32 quaternion_xyzw[4]

uint16 magnetic_count
uint16 reserved = 0

repeated magnetic_count times:
    uint32 sensor_sequence
    uint64 mcu_time_us
    float64 phone_receive_unix
    float64 phone_receive_monotonic
    float32 sensor[5][4]

uint32 crc32
```

The fixed data before magnetic samples is 88 bytes, each magnetic sample is
108 bytes, and the trailing CRC is 4 bytes. At most ten magnetic samples are
included, so the largest datagram is 1172 bytes. CRC uses standard
CRC-32/ISO-HDLC over every preceding byte.

Each AR pose drains magnetic samples received up to that AR frame time. If pose
frames stop for 50 ms, the app sends magnetic-only packets with flag bit 0
cleared. A missing computer only drops the live forwarding copy; the capture
continues on the phone.

## Low-Latency Video Stream

The original 1x low-latency video stream uses UDP port `5560`. A separate 0.5x ultra-wide APV2 stream for ArUco processing uses UDP port `5561` at approximately 10 FPS. Both carry H.264 NAL units without MP4/RTSP/WebRTC container overhead. New senders use `APV2`; receivers remain backward compatible with `APV1`.

The 0.5x stream follows the research-only UMI-FT approach and reads ARKit's private ultra-wide `ARFrame` storage on supported iPhone Pro devices. It does not alter the primary `capturedImage`, preview, pose, or 1x recording. Because the fields are private API, this build is intended for development signing/sideloading rather than App Store distribution, and iOS updates may require compatibility changes.

Common packet header layout:

1. `magic` as ASCII `APV1` or `APV2`
2. `version` as `UInt8`
3. `flags` as `UInt8`
4. `reserved` as `UInt16`
5. `frame_id` as `UInt32`
6. `capture_timestamp` as Unix time in a `Float64`
7. `nalu_index` as `UInt16`
8. `nalu_count` as `UInt16`
9. `fragment_index` as `UInt16`
10. `fragment_count` as `UInt16`

The legacy `APV1` header ends here and is 28 bytes. `APV2` appends the camera calibration used by the captured image:

11. `fx` as `Float32`
12. `fy` as `Float32`
13. `cx` as `Float32`
14. `cy` as `Float32`
15. calibration image width as `UInt16`
16. calibration image height as `UInt16`

The raw H.264 NAL payload starts immediately after the applicable 28-byte or 48-byte header.

Header sizes:

```text
APV1: 28 bytes, magic="APV1", version=1
APV2: 48 bytes, magic="APV2", version=2
```

All numeric fields are little-endian. A receiver scales `fx`, `fy`, `cx`, and `cy` by decoded-resolution/calibration-resolution before using them. The APV2 intrinsics make metric fiducial pose estimation possible without guessing the iPhone field of view.

Flags:

- bit `0`: key frame
- bit `1`: packet belongs to an SPS/PPS parameter set

Sender behavior:

- selectable capture targets are 480p (`640x480`), 720p (`1280x720`), and 1080p (`1920x1080`), subject to the closest format supported by the device
- target packet size is `<= 1200` bytes to avoid IP fragmentation
- H.264 is encoded with `VideoToolbox` in real-time mode with frame reordering disabled
- SPS/PPS is repeated on each key frame so receivers can recover quickly after packet loss
- dropped packets are not retransmitted; the receiver should prefer dropping an incomplete frame over building up latency
- one-way latency must account for sender/receiver wall-clock offset; the debug receiver estimates that offset from the lowest-delay pose packets (or video arrival as a fallback)

## ArUco Gripper Distance Output (AGP1)

The integrated monitor detects the two configured ArUco markers from the 0.5x APV2 stream on UDP `5561` and sends one UTF-8 JSON datagram per processed frame, normally to UDP port `5570`. This path uses the ultra-wide APV2 camera intrinsics but does not use the AR pose stream or any robot base/TCP transform.

For a valid frame, `status` is `tracking_gripper_distance` and `gripper_distance` contains:

- `raw_marker_x_distance_m`: absolute difference between the two marker-center X coordinates in the camera frame; this is the value used for calibration
- `marker_center_distance_3d_m`: Euclidean center distance for diagnostics only
- `calibrated_m` / `calibrated_mm`: jaw gap after two-point scale and offset calibration
- `filtered_m` / `filtered_mm`: optional EMA-filtered jaw gap
- `scale` and `offset_m`: active linear calibration
- `calibration_complete`: true only after both calibration endpoints are available
- `calibrated_range_mm`: minimum and maximum jaw gaps used for calibration

The calibration is:

```text
actual_gap = scale * raw_marker_x_distance + offset
```

Both markers are required in every valid frame. Their depths must also fall inside the configured nominal-depth window (UMI default: `0.072 ± 0.008 m`); otherwise the status is `marker_depth_out_of_range`. Consumers must use a distance only when the status is `tracking_gripper_distance` and `calibration_complete` is true. Before calibration, `raw_marker_x_distance_m` remains available so the UI can collect repeated open-close cycles.

## Unified Experiment Capture

Live pose, sensor, and low-latency video monitoring can remain active independently of experiment recording. A recording button creates one experiment UUID and one phone monotonic-clock origin. Only samples between the experiment start and stop events are persisted.

The app sends JSON control events to `POST /experiment/control` on the upload server:

```json
{
  "event": "start",
  "experimentID": "UUID",
  "eventUnixTime": 1700000000.0,
  "eventMonotonicTime": 12345.0
}
```

`stop` uses the same shape and UUID. The App's **End & Delete** action sends
`discard` instead; both phone-side capture files and the matching incomplete
computer-side experiment directory are removed, and no capture is uploaded.
After a normal stop finalizes the MP4, the app uploads all available components
with `X-Upload-Kind: experiment`, the shared UUID as `X-Capture-ID`, and the
start time as `X-Experiment-Start-Unix-Time`. The computer stores one
timestamp-named directory per experiment (for example `20260714-205900`); the
UUID remains in its metadata. Each directory contains:

- `pose.csv`
- `magnetic.csv` when sensor samples were available
- `video.mp4` when video recording succeeded
- `sender_transport.csv`
- `receiver_transport.csv` when the integrated monitor observed the live session
- `capture_manifest.json`
- `dataset.zarr` after the automatic background conversion completes
- `upload_state.json` and `experiment_state.json`

## Wired Sensor Mirror

The iPhone app can also mirror a supported wired sensor stream to the host for validation.

Assumptions:

- the sensor accessory is visible through iOS `ExternalAccessory`
- the accessory exposes the protocol configured in `UISupportedExternalAccessoryProtocols`
- the serial stream is UTF-8 text with one sample per line

Accepted line formats:

```text
AP2,2,source,seq,t,x,y,z,qx,qy,qz,qw,checksum
seq,t,x,y,z,qx,qy,qz,qw
t,x,y,z,qx,qy,qz,qw
x,y,z,qx,qy,qz,qw
```

Quaternion order is `xyzw`. Commas, semicolons, spaces, and tabs are accepted as delimiters.
The recommended `AP2` checksum is FNV-1a UInt32 over the comma-joined payload before the checksum field, for example `AP2,2,imu,42,12.345,...`.

The mirrored UDP payload defaults to port `5556` and now uses an `APS2` binary packet when sent by the iPhone app:

```text
magic="APS2", version=2, flags, sequence, sensor_time, iphone_receive_time,
x, y, z, qx, qy, qz, qw, checksum
```

The checksum is FNV-1a UInt32 over all bytes before the checksum field. Legacy 40-byte packets are still accepted by the desktop validator.
For mirrored sensor packets, `iphone_receive_time` is always present. If the serial line includes `t`, it is preserved as `sensor_time` for drift-aware calibration.
