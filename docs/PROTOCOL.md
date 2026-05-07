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
