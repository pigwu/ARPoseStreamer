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
