#!/usr/bin/env python3
"""Send deterministic ASKN v1 packets for iPhone hotspot testing.

This emulates the five-chip magnetic board and is useful before the MCU
firmware is available. The destination should be the iPhone hotspot gateway.
"""

from __future__ import annotations

import argparse
import math
import socket
import struct
import time


ASKN_MAGIC = 0x41534B4E
ASKN_PACKET = struct.Struct("<IIQ20f")
DEFAULT_HOST = "172.20.10.1"
DEFAULT_PORT = 5557


def build_packet(sequence: int, mcu_time_us: int, elapsed: float) -> bytes:
    values = []
    for chip_index in range(5):
        phase = elapsed * (1.1 + chip_index * 0.08) + chip_index * 0.55
        temperature = 24.0 + chip_index * 0.2 + 0.1 * math.sin(phase * 0.2)
        x = 10.0 * math.sin(phase)
        y = 8.0 * math.cos(phase * 0.83)
        z = 6.0 * math.sin(phase * 0.61 + 0.3)
        values.extend((temperature, x, y, z))

    return ASKN_PACKET.pack(
        ASKN_MAGIC,
        sequence & 0xFFFFFFFF,
        mcu_time_us & 0xFFFFFFFFFFFFFFFF,
        *values,
    )


def run_sender(host: str, port: int, rate: float, count: int) -> int:
    interval = 1.0 / rate
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    started = time.monotonic()
    next_send = started
    sequence = 0
    sent_bytes = 0

    print(f"[INFO] Sending ASKN v1 to {host}:{port} at {rate:g} Hz")
    print("[INFO] Press Ctrl+C to stop")

    try:
        while count <= 0 or sequence < count:
            now = time.monotonic()
            if now < next_send:
                time.sleep(min(next_send - now, interval))
                continue

            elapsed = now - started
            packet = build_packet(sequence, int(elapsed * 1_000_000), elapsed)
            sent_bytes += sock.sendto(packet, (host, port))
            sequence += 1
            next_send += interval

            # If the process was paused, resume from the current point instead
            # of emitting a large burst of stale samples.
            if now - next_send > interval * 4:
                next_send = now + interval
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()

    duration = max(time.monotonic() - started, 1e-9)
    measured_rate = max(sequence - 1, 0) / duration
    print(
        f"[INFO] Sent {sequence} packets / {sent_bytes} bytes "
        f"in {duration:.2f}s ({measured_rate:.1f} Hz)"
    )
    return 0


def self_test() -> int:
    packet = build_packet(42, 1_234_567, 0.5)
    if len(packet) != 96:
        raise AssertionError(f"expected 96 bytes, got {len(packet)}")
    unpacked = ASKN_PACKET.unpack(packet)
    if unpacked[0] != ASKN_MAGIC or unpacked[1] != 42 or unpacked[2] != 1_234_567:
        raise AssertionError("ASKN header round-trip failed")
    if packet[:4] != bytes((0x4E, 0x4B, 0x53, 0x41)):
        raise AssertionError(f"unexpected ASKN wire magic: {packet[:4].hex()}")
    if len(unpacked[3:]) != 20 or not all(math.isfinite(value) for value in unpacked[3:]):
        raise AssertionError("ASKN values are invalid")
    print("[SELF-TEST] PASS: 96-byte <IIQ20f packet and little-endian magic verified")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Emulate the AnySkin board over UDP")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Phone/gateway IP")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Phone UDP port")
    parser.add_argument("--rate", type=float, default=100.0, help="Packet rate in Hz")
    parser.add_argument("--count", type=int, default=0, help="Stop after N packets; 0 runs forever")
    parser.add_argument("--self-test", action="store_true", help="Validate one packet and exit")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not math.isfinite(args.rate) or args.rate <= 0 or args.rate > 1000:
        parser.error("--rate must be greater than 0 and no more than 1000 Hz")
    if args.count < 0:
        parser.error("--count cannot be negative")
    return run_sender(args.host, args.port, args.rate, args.count)


if __name__ == "__main__":
    raise SystemExit(main())
