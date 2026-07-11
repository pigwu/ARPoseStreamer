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

The optional low-latency video stream uses a second UDP port, default `5560`, and carries H.264 NAL units without MP4/RTSP/WebRTC container overhead.

Packet header layout:

1. `magic` as ASCII `APV1`
2. `version` as `UInt8`
3. `flags` as `UInt8`
4. `reserved` as `UInt16`
5. `frame_id` as `UInt32`
6. `capture_timestamp` as `Float64`
7. `nalu_index` as `UInt16`
8. `nalu_count` as `UInt16`
9. `fragment_index` as `UInt16`
10. `fragment_count` as `UInt16`
11. `payload` as raw H.264 NAL bytes

Header size:

```text
28 bytes
```

Flags:

- bit `0`: key frame
- bit `1`: packet belongs to an SPS/PPS parameter set

Sender behavior:

- target packet size is `<= 1200` bytes to avoid IP fragmentation
- H.264 is encoded with `VideoToolbox` in real-time mode with frame reordering disabled
- SPS/PPS is repeated on each key frame so receivers can recover quickly after packet loss
- dropped packets are not retransmitted; the receiver should prefer dropping an incomplete frame over building up latency

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
