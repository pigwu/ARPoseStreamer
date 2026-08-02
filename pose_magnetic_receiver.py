#!/usr/bin/env python3
"""Receive combined AR pose and magnetic sensor packets from the iPhone.

APM2 keeps the APM1 header and adds a board-side field to every magnetic
sample.  APM1 remains accepted for recordings made by the single-board test
version and is interpreted as right-board data.  Values are little-endian::

    4s magic (``APM1`` or ``APM2``)
    uint16 version (1 or 2)
    uint16 flags (bit 0: pose is present)
    uint32 packet_sequence
    uint8 session_uuid[16]
    double phone_send_unix
    uint32 pose_sequence
    double pose_sender_unix
    double pose_frame_monotonic
    float32 position_xyz[3]
    float32 quaternion_xyzw[4]
    uint16 magnetic_count
    uint16 reserved (0)
    magnetic_sample[magnetic_count]
    uint32 crc32

Each APM2 magnetic sample starts with ``uint32 board_side`` (0 right, 1 left),
followed by ``uint32 sequence``, ``uint64 mcu_time_us``, two doubles for the
phone receive Unix/monotonic times, and 20 float32 values (five sensors, each
ordered t/x/y/z).  APM1 omits ``board_side``.  The CRC is zlib CRC32 over every
byte before the final CRC field.

``decode_apm1_packet`` is intentionally independent from the receiver so it
can be imported by tests and other desktop tools.
"""

from __future__ import annotations

import argparse
import csv
import math
import signal
import socket
import struct
import sys
import time
import uuid
import zlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Sequence, TextIO, Tuple


MAGIC = b"APM1"
PROTOCOL_VERSION = 1
APM2_MAGIC = b"APM2"
APM2_PROTOCOL_VERSION = 2
RIGHT_BOARD = "right"
LEFT_BOARD = "left"
BOARD_SIDE_NAMES = {0: RIGHT_BOARD, 1: LEFT_BOARD}
POSE_PRESENT_FLAG = 0x0001
KNOWN_FLAGS = POSE_PRESENT_FLAG
DEFAULT_HOST = "0.0.0.0"
DEFAULT_COMBINED_PORT = 5558
DEFAULT_REGISTRATION_PORT = 5559
DEFAULT_VIDEO_PORT = 5560

# Fixed APM1/APM2 header, magnetic sample layouts, and trailing checksum.
HEADER_STRUCT = struct.Struct("<4sHHI16sdIdd7fHH")
MAGNETIC_SAMPLE_STRUCT = struct.Struct("<IQdd20f")
APM2_MAGNETIC_SAMPLE_STRUCT = struct.Struct("<IIQdd20f")
CRC_STRUCT = struct.Struct("<I")
MIN_PACKET_SIZE = HEADER_STRUCT.size + CRC_STRUCT.size
UINT32_MASK = 0xFFFFFFFF
UINT32_HALF_RANGE = 0x80000000


class APM1DecodeError(ValueError):
    """Base class for invalid APM1 datagrams."""


class APM1FormatError(APM1DecodeError):
    """The datagram does not conform to the APM1 layout."""


class APM1CRCError(APM1DecodeError):
    """The datagram has a valid layout but failed its CRC check."""


@dataclass(frozen=True)
class PoseSample:
    sequence: int
    sender_unix: float
    frame_monotonic: float
    position: Tuple[float, float, float]
    quaternion: Tuple[float, float, float, float]


@dataclass(frozen=True)
class MagneticSample:
    side: str
    sequence: int
    mcu_time_us: int
    phone_receive_unix: float
    phone_receive_monotonic: float
    values: Tuple[float, ...]

    def sensors(self) -> Tuple[Tuple[float, float, float, float], ...]:
        """Return the flat values as five ``(t, x, y, z)`` tuples."""
        return tuple(
            tuple(self.values[index : index + 4])  # type: ignore[arg-type]
            for index in range(0, len(self.values), 4)
        )


@dataclass(frozen=True)
class APM1Packet:
    version: int
    flags: int
    packet_sequence: int
    session_id: uuid.UUID
    phone_send_unix: float
    pose: Optional[PoseSample]
    magnetic_samples: Tuple[MagneticSample, ...]
    crc32: int


def decode_apm_packet(datagram: bytes) -> APM1Packet:
    """Decode and validate one complete APM1 or APM2 UDP datagram.

    Raises:
        APM1FormatError: if magic, version, flags, reserved data, or size is
            invalid.
        APM1CRCError: if the trailing CRC32 does not match the payload.
    """

    if not isinstance(datagram, (bytes, bytearray, memoryview)):
        raise TypeError("datagram must be a bytes-like object")
    packet = bytes(datagram)
    if len(packet) < MIN_PACKET_SIZE:
        raise APM1FormatError(
            f"packet is too short: expected at least {MIN_PACKET_SIZE} bytes, got {len(packet)}"
        )

    try:
        header = HEADER_STRUCT.unpack_from(packet, 0)
    except struct.error as exc:  # Defensive; the minimum-size check normally catches this.
        raise APM1FormatError(f"cannot unpack APM header: {exc}") from exc

    (
        magic,
        version,
        flags,
        packet_sequence,
        session_bytes,
        phone_send_unix,
        pose_sequence,
        pose_sender_unix,
        pose_frame_monotonic,
        px,
        py,
        pz,
        qx,
        qy,
        qz,
        qw,
        magnetic_count,
        reserved,
    ) = header

    if (magic, version) == (MAGIC, PROTOCOL_VERSION):
        magnetic_struct = MAGNETIC_SAMPLE_STRUCT
        protocol_name = "APM1"
    elif (magic, version) == (APM2_MAGIC, APM2_PROTOCOL_VERSION):
        magnetic_struct = APM2_MAGNETIC_SAMPLE_STRUCT
        protocol_name = "APM2"
    else:
        raise APM1FormatError(
            f"unsupported APM magic/version pair: {magic!r} version {version}"
        )
    if flags & ~KNOWN_FLAGS:
        raise APM1FormatError(f"unknown APM1 flags: 0x{flags:04x}")
    if reserved != 0:
        raise APM1FormatError(f"reserved header field must be zero, got {reserved}")

    expected_size = (
        HEADER_STRUCT.size
        + magnetic_count * magnetic_struct.size
        + CRC_STRUCT.size
    )
    if len(packet) != expected_size:
        raise APM1FormatError(
            f"wrong packet size for magnetic_count={magnetic_count}: "
            f"expected {expected_size} bytes, got {len(packet)}"
        )

    transmitted_crc = CRC_STRUCT.unpack_from(packet, len(packet) - CRC_STRUCT.size)[0]
    calculated_crc = zlib.crc32(packet[:-CRC_STRUCT.size]) & UINT32_MASK
    if transmitted_crc != calculated_crc:
        raise APM1CRCError(
            f"CRC mismatch: expected 0x{transmitted_crc:08x}, "
            f"calculated 0x{calculated_crc:08x}"
        )

    pose: Optional[PoseSample]
    if flags & POSE_PRESENT_FLAG:
        pose = PoseSample(
            sequence=pose_sequence,
            sender_unix=pose_sender_unix,
            frame_monotonic=pose_frame_monotonic,
            position=(px, py, pz),
            quaternion=(qx, qy, qz, qw),
        )
    else:
        pose = None

    samples: List[MagneticSample] = []
    offset = HEADER_STRUCT.size
    for _ in range(magnetic_count):
        unpacked = magnetic_struct.unpack_from(packet, offset)
        if protocol_name == "APM2":
            side_value = unpacked[0]
            if side_value not in BOARD_SIDE_NAMES:
                raise APM1FormatError(f"unknown APM2 board side: {side_value}")
            side = BOARD_SIDE_NAMES[side_value]
            value_offset = 1
        else:
            side = RIGHT_BOARD
            value_offset = 0
        samples.append(
            MagneticSample(
                side=side,
                sequence=unpacked[value_offset],
                mcu_time_us=unpacked[value_offset + 1],
                phone_receive_unix=unpacked[value_offset + 2],
                phone_receive_monotonic=unpacked[value_offset + 3],
                values=tuple(unpacked[value_offset + 4 :]),
            )
        )
        offset += magnetic_struct.size

    return APM1Packet(
        version=version,
        flags=flags,
        packet_sequence=packet_sequence,
        session_id=uuid.UUID(bytes=session_bytes),
        phone_send_unix=phone_send_unix,
        pose=pose,
        magnetic_samples=tuple(samples),
        crc32=transmitted_crc,
    )


def decode_apm1_packet(datagram: bytes) -> APM1Packet:
    """Compatibility entry point that now accepts both APM1 and APM2."""
    return decode_apm_packet(datagram)


def decode_packet(datagram: bytes) -> APM1Packet:
    """Compatibility-friendly short name for :func:`decode_apm_packet`."""
    return decode_apm_packet(datagram)


@dataclass
class SequenceTracker:
    """Track gaps, duplicates, and reordering for one uint32 sequence."""

    last: Optional[int] = None
    received: int = 0
    missing: int = 0
    duplicates: int = 0
    out_of_order: int = 0

    def observe(self, sequence: int) -> None:
        sequence &= UINT32_MASK
        self.received += 1
        if self.last is None:
            self.last = sequence
            return

        distance = (sequence - self.last) & UINT32_MASK
        if distance == 0:
            self.duplicates += 1
        elif distance < UINT32_HALF_RANGE:
            self.missing += distance - 1
            self.last = sequence
        else:
            self.out_of_order += 1

    @property
    def loss_percent(self) -> float:
        denominator = self.received + self.missing
        return (self.missing * 100.0 / denominator) if denominator else 0.0


def _sum_tracker_field(trackers: Iterable[SequenceTracker], name: str) -> int:
    return sum(int(getattr(tracker, name)) for tracker in trackers)


@dataclass
class ReceiverStats:
    started_monotonic: float = field(default_factory=time.monotonic)
    valid_packets: int = 0
    pose_samples: int = 0
    magnetic_samples: int = 0
    crc_errors: int = 0
    format_errors: int = 0
    socket_errors: int = 0
    hello_sent: int = 0
    hello_errors: int = 0
    last_source: Optional[Tuple[str, int]] = None
    last_latency_ms: Optional[float] = None
    recent_packet_times: Deque[float] = field(default_factory=deque)
    packet_trackers: Dict[uuid.UUID, SequenceTracker] = field(default_factory=dict)
    pose_trackers: Dict[uuid.UUID, SequenceTracker] = field(default_factory=dict)
    magnetic_trackers: Dict[Tuple[uuid.UUID, str], SequenceTracker] = field(
        default_factory=dict
    )

    def observe(
        self,
        packet: APM1Packet,
        source: Tuple[str, int],
        receive_unix: float,
        receive_monotonic: float,
    ) -> None:
        self.valid_packets += 1
        self.last_source = source
        if math.isfinite(packet.phone_send_unix):
            self.last_latency_ms = (receive_unix - packet.phone_send_unix) * 1000.0
        self.recent_packet_times.append(receive_monotonic)
        self._trim_rate_window(receive_monotonic)

        self.packet_trackers.setdefault(packet.session_id, SequenceTracker()).observe(
            packet.packet_sequence
        )
        if packet.pose is not None:
            self.pose_samples += 1
            self.pose_trackers.setdefault(packet.session_id, SequenceTracker()).observe(
                packet.pose.sequence
            )
        for sample in packet.magnetic_samples:
            self.magnetic_samples += 1
            self.magnetic_trackers.setdefault(
                (packet.session_id, sample.side), SequenceTracker()
            ).observe(sample.sequence)

    def _trim_rate_window(self, now: float) -> None:
        while self.recent_packet_times and now - self.recent_packet_times[0] > 5.0:
            self.recent_packet_times.popleft()

    def packet_rate(self, now: Optional[float] = None) -> float:
        self._trim_rate_window(time.monotonic() if now is None else now)
        if len(self.recent_packet_times) < 2:
            return 0.0
        duration = self.recent_packet_times[-1] - self.recent_packet_times[0]
        return (len(self.recent_packet_times) - 1) / max(duration, 1e-9)

    def tracker_totals(self, tracker_name: str) -> Tuple[int, int, int]:
        trackers: Iterable[SequenceTracker] = getattr(self, tracker_name).values()
        tracker_list = list(trackers)
        return (
            _sum_tracker_field(tracker_list, "missing"),
            _sum_tracker_field(tracker_list, "duplicates"),
            _sum_tracker_field(tracker_list, "out_of_order"),
        )


class CSVRecorder:
    """Write accepted APM data to pose plus independent right/left CSV files."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir.expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.pose_path = self.output_dir / "pose.csv"
        self.magnetic_path = self.output_dir / "magnetic_right.csv"
        self.left_magnetic_path = self.output_dir / "magnetic_left.csv"
        self.pose_file: TextIO = self.pose_path.open("w", newline="", encoding="utf-8")
        try:
            self.magnetic_file: TextIO = self.magnetic_path.open(
                "w", newline="", encoding="utf-8"
            )
            self.left_magnetic_file: TextIO = self.left_magnetic_path.open(
                "w", newline="", encoding="utf-8"
            )
        except Exception:
            self.pose_file.close()
            if hasattr(self, "magnetic_file"):
                self.magnetic_file.close()
            raise

        self.pose_writer = csv.writer(self.pose_file)
        self.magnetic_writer = csv.writer(self.magnetic_file)
        self.left_magnetic_writer = csv.writer(self.left_magnetic_file)
        self._last_flush = time.monotonic()
        self._write_headers()

    def _write_headers(self) -> None:
        common = [
            "pc_receive_unix",
            "pc_receive_monotonic",
            "source_ip",
            "source_port",
            "session_id",
            "packet_sequence",
            "phone_send_unix",
            "phone_to_pc_latency_ms",
        ]
        self.pose_writer.writerow(
            common
            + [
                "pose_sequence",
                "pose_sender_unix",
                "pose_frame_monotonic",
                "x",
                "y",
                "z",
                "qx",
                "qy",
                "qz",
                "qw",
            ]
        )
        magnetic_header = common + [
            "sample_index_in_packet",
            "sensor_sequence",
            "mcu_time_us",
            "phone_receive_unix",
            "phone_receive_monotonic",
            "phone_receive_to_pc_latency_ms",
        ]
        for sensor_index in range(5):
            magnetic_header.extend(
                [
                    f"s{sensor_index}_t",
                    f"s{sensor_index}_x",
                    f"s{sensor_index}_y",
                    f"s{sensor_index}_z",
                ]
            )
        self.magnetic_writer.writerow(magnetic_header)
        self.left_magnetic_writer.writerow(magnetic_header)

    def write_packet(
        self,
        packet: APM1Packet,
        source: Tuple[str, int],
        receive_unix: float,
        receive_monotonic: float,
    ) -> None:
        phone_latency_ms = (receive_unix - packet.phone_send_unix) * 1000.0
        common: List[object] = [
            f"{receive_unix:.9f}",
            f"{receive_monotonic:.9f}",
            source[0],
            source[1],
            str(packet.session_id),
            packet.packet_sequence,
            f"{packet.phone_send_unix:.9f}",
            f"{phone_latency_ms:.3f}",
        ]

        if packet.pose is not None:
            pose = packet.pose
            self.pose_writer.writerow(
                common
                + [
                    pose.sequence,
                    f"{pose.sender_unix:.9f}",
                    f"{pose.frame_monotonic:.9f}",
                    *pose.position,
                    *pose.quaternion,
                ]
            )

        for sample_index, sample in enumerate(packet.magnetic_samples):
            sample_latency_ms = (receive_unix - sample.phone_receive_unix) * 1000.0
            writer = (
                self.magnetic_writer
                if sample.side == RIGHT_BOARD
                else self.left_magnetic_writer
            )
            writer.writerow(
                common
                + [
                    sample_index,
                    sample.sequence,
                    sample.mcu_time_us,
                    f"{sample.phone_receive_unix:.9f}",
                    f"{sample.phone_receive_monotonic:.9f}",
                    f"{sample_latency_ms:.3f}",
                    *sample.values,
                ]
            )
        self.flush_if_due(receive_monotonic)

    def flush_if_due(self, now: Optional[float] = None, force: bool = False) -> None:
        current = time.monotonic() if now is None else now
        if force or current - self._last_flush >= 1.0:
            self.pose_file.flush()
            self.magnetic_file.flush()
            self.left_magnetic_file.flush()
            self._last_flush = current

    def close(self) -> None:
        self.flush_if_due(force=True)
        self.pose_file.close()
        self.magnetic_file.close()
        self.left_magnetic_file.close()

    def __enter__(self) -> "CSVRecorder":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:  # type: ignore[no-untyped-def]
        self.close()


_stop_requested = False


def _handle_signal(signum: int, frame: object) -> None:
    del signum, frame
    global _stop_requested
    _stop_requested = True


def _error_should_be_printed(error_count: int) -> bool:
    return error_count <= 5 or error_count % 100 == 0


def _format_status(stats: ReceiverStats) -> str:
    packet_missing, packet_duplicates, packet_ooo = stats.tracker_totals(
        "packet_trackers"
    )
    magnetic_missing, magnetic_duplicates, magnetic_ooo = stats.tracker_totals(
        "magnetic_trackers"
    )
    pose_missing, pose_duplicates, pose_ooo = stats.tracker_totals("pose_trackers")
    packet_denominator = stats.valid_packets + packet_missing
    packet_loss = (
        packet_missing * 100.0 / packet_denominator if packet_denominator else 0.0
    )
    magnetic_denominator = stats.magnetic_samples + magnetic_missing
    magnetic_loss = (
        magnetic_missing * 100.0 / magnetic_denominator
        if magnetic_denominator
        else 0.0
    )
    pose_denominator = stats.pose_samples + pose_missing
    pose_loss = pose_missing * 100.0 / pose_denominator if pose_denominator else 0.0
    source = (
        f"{stats.last_source[0]}:{stats.last_source[1]}"
        if stats.last_source is not None
        else "--"
    )
    latency = (
        f"{stats.last_latency_ms:+.1f}ms"
        if stats.last_latency_ms is not None
        else "--"
    )
    return (
        f"[STAT] rate={stats.packet_rate():.1f}pkt/s valid={stats.valid_packets} "
        f"packet_loss={packet_loss:.3f}%({packet_missing}) "
        f"packet_dup/ooo={packet_duplicates}/{packet_ooo} "
        f"pose={stats.pose_samples} pose_loss={pose_loss:.3f}%({pose_missing}) "
        f"pose_dup/ooo={pose_duplicates}/{pose_ooo} mag={stats.magnetic_samples} "
        f"mag_loss={magnetic_loss:.3f}%({magnetic_missing}) "
        f"mag_dup/ooo={magnetic_duplicates}/{magnetic_ooo} "
        f"crc/format={stats.crc_errors}/{stats.format_errors} "
        f"source={source} latency={latency}"
    )


def _default_output_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path.cwd() / "logs" / f"pose_magnetic_{stamp}"


def run_receiver(args: argparse.Namespace) -> int:
    """Run the UDP receive loop using parsed command-line arguments."""

    global _stop_requested
    _stop_requested = False
    stats = ReceiverStats()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.host, args.port))
    except OSError as exc:
        sock.close()
        print(f"[ERROR] Cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 2
    sock.settimeout(0.25)

    output_dir = args.output_dir if args.output_dir is not None else _default_output_dir()
    try:
        recorder = CSVRecorder(output_dir)
    except OSError as exc:
        sock.close()
        print(f"[ERROR] Cannot create CSV logs in {output_dir}: {exc}", file=sys.stderr)
        return 2

    hello_payload = f"PC_HELLO,1,{args.port},{args.video_port}\n".encode("ascii")
    next_hello = time.monotonic()
    next_status = time.monotonic() + args.status_interval

    print(f"[INFO] Listening for APM1/APM2 UDP on {args.host}:{args.port}")
    print(f"[INFO] Pose CSV: {recorder.pose_path}")
    print(f"[INFO] Right magnetic CSV: {recorder.magnetic_path}")
    print(f"[INFO] Left magnetic CSV: {recorder.left_magnetic_path}")
    if args.phone_ip:
        print(
            f"[INFO] Registering with phone at {args.phone_ip}:"
            f"{args.registration_port} every {args.hello_interval:g}s"
        )
    else:
        print("[INFO] No phone/gateway IP supplied; registration is disabled")
    print("[INFO] Press Ctrl+C to stop")

    exit_code = 0
    try:
        while not _stop_requested:
            now = time.monotonic()
            if args.phone_ip and now >= next_hello:
                try:
                    sock.sendto(
                        hello_payload, (args.phone_ip, args.registration_port)
                    )
                    stats.hello_sent += 1
                except OSError as exc:
                    stats.hello_errors += 1
                    if _error_should_be_printed(stats.hello_errors):
                        print(f"[WARN] PC_HELLO send failed: {exc}", file=sys.stderr)
                next_hello = now + args.hello_interval

            received_datagram: Optional[bytes]
            try:
                datagram, source = sock.recvfrom(65535)
                received_datagram = datagram
            except socket.timeout:
                received_datagram = None
                source = ("", 0)
            except OSError as exc:
                if _stop_requested:
                    break
                stats.socket_errors += 1
                print(f"[ERROR] UDP receive failed: {exc}", file=sys.stderr)
                exit_code = 1
                break

            if received_datagram is not None:
                receive_unix = time.time()
                receive_monotonic = time.monotonic()
                try:
                    decoded = decode_apm_packet(received_datagram)
                except APM1CRCError as exc:
                    stats.crc_errors += 1
                    if _error_should_be_printed(stats.crc_errors):
                        print(
                            f"[WARN] CRC error from {source[0]}:{source[1]}: {exc}",
                            file=sys.stderr,
                        )
                except APM1DecodeError as exc:
                    stats.format_errors += 1
                    if _error_should_be_printed(stats.format_errors):
                        print(
                            f"[WARN] Format error from {source[0]}:{source[1]}: {exc}",
                            file=sys.stderr,
                        )
                else:
                    stats.observe(decoded, source, receive_unix, receive_monotonic)
                    try:
                        recorder.write_packet(
                            decoded, source, receive_unix, receive_monotonic
                        )
                    except OSError as exc:
                        print(f"[ERROR] CSV write failed: {exc}", file=sys.stderr)
                        exit_code = 1
                        break

            now = time.monotonic()
            recorder.flush_if_due(now)
            if now >= next_status:
                print(_format_status(stats))
                next_status = now + args.status_interval
    except KeyboardInterrupt:
        _stop_requested = True
    finally:
        print("\n[INFO] Stopping receiver...")
        recorder.close()
        sock.close()
        print(_format_status(stats))
        if args.phone_ip:
            print(
                f"[INFO] PC_HELLO sent/errors: "
                f"{stats.hello_sent}/{stats.hello_errors}"
            )
        print(f"[INFO] CSV files saved in: {recorder.output_dir}")

    return exit_code


def _build_self_test_packet() -> Tuple[bytes, uuid.UUID]:
    """Construct a deterministic valid datagram for the built-in smoke test."""

    session_id = uuid.UUID("12345678-1234-5678-9abc-def012345678")
    pose_values: Sequence[float] = (1.0, -2.0, 3.5, 0.1, 0.2, 0.3, 0.9)
    magnetic_rows = []
    for sample_index in range(2):
        values = tuple(float(sample_index * 100 + value_index) for value_index in range(20))
        magnetic_rows.append(
            MAGNETIC_SAMPLE_STRUCT.pack(
                1000 + sample_index,
                8_000_000 + sample_index * 10_000,
                1_700_000_000.1 + sample_index * 0.01,
                1234.5 + sample_index * 0.01,
                *values,
            )
        )

    payload = HEADER_STRUCT.pack(
        MAGIC,
        PROTOCOL_VERSION,
        POSE_PRESENT_FLAG,
        42,
        session_id.bytes,
        1_700_000_000.2,
        77,
        1_700_000_000.19,
        1234.59,
        *pose_values,
        len(magnetic_rows),
        0,
    ) + b"".join(magnetic_rows)
    return payload + CRC_STRUCT.pack(zlib.crc32(payload) & UINT32_MASK), session_id


def _build_apm2_self_test_packet() -> Tuple[bytes, uuid.UUID]:
    """Construct one right and one left APM2 sample with equal sequences."""
    session_id = uuid.UUID("87654321-4321-8765-cba9-876543210fed")
    rows = []
    for side_value in (0, 1):
        values = tuple(float(side_value * 100 + index) for index in range(20))
        rows.append(
            APM2_MAGNETIC_SAMPLE_STRUCT.pack(
                side_value,
                25,
                9_000_000 + side_value,
                1_700_000_001.0 + side_value * 0.01,
                2000.0 + side_value * 0.01,
                *values,
            )
        )
    payload = HEADER_STRUCT.pack(
        APM2_MAGIC,
        APM2_PROTOCOL_VERSION,
        0,
        9,
        session_id.bytes,
        1_700_000_001.1,
        0,
        0.0,
        0.0,
        *(0.0 for _ in range(7)),
        len(rows),
        0,
    ) + b"".join(rows)
    return payload + CRC_STRUCT.pack(zlib.crc32(payload) & UINT32_MASK), session_id


def run_self_test() -> int:
    """Decode legacy APM1 and dual-board APM2, then verify CRC rejection."""

    datagram, expected_session = _build_self_test_packet()
    decoded = decode_apm1_packet(datagram)
    assert decoded.session_id == expected_session
    assert decoded.packet_sequence == 42
    assert decoded.pose is not None and decoded.pose.sequence == 77
    assert len(decoded.magnetic_samples) == 2
    assert all(sample.side == RIGHT_BOARD for sample in decoded.magnetic_samples)
    assert decoded.magnetic_samples[0].sequence == 1000
    assert decoded.magnetic_samples[1].values[-1] == 119.0
    assert len(datagram) == MIN_PACKET_SIZE + 2 * MAGNETIC_SAMPLE_STRUCT.size

    # The board is optional during experiments: pose-only APM1 packets must
    # remain valid and decodable with magnetic_count == 0.
    pose_only_payload = HEADER_STRUCT.pack(
        MAGIC,
        PROTOCOL_VERSION,
        POSE_PRESENT_FLAG,
        43,
        expected_session.bytes,
        1_700_000_000.3,
        78,
        1_700_000_000.29,
        1234.69,
        1.0,
        2.0,
        3.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0,
        0,
    )
    pose_only = pose_only_payload + CRC_STRUCT.pack(
        zlib.crc32(pose_only_payload) & UINT32_MASK
    )
    decoded_pose_only = decode_apm1_packet(pose_only)
    assert decoded_pose_only.pose is not None
    assert decoded_pose_only.pose.sequence == 78
    assert not decoded_pose_only.magnetic_samples
    assert len(pose_only) == MIN_PACKET_SIZE

    apm2_datagram, apm2_session = _build_apm2_self_test_packet()
    decoded_apm2 = decode_apm_packet(apm2_datagram)
    assert decoded_apm2.session_id == apm2_session
    assert [sample.side for sample in decoded_apm2.magnetic_samples] == [
        RIGHT_BOARD,
        LEFT_BOARD,
    ]
    dual_stats = ReceiverStats()
    dual_stats.observe(decoded_apm2, ("127.0.0.1", 5558), time.time(), time.monotonic())
    assert len(dual_stats.magnetic_trackers) == 2
    assert all(tracker.missing == 0 for tracker in dual_stats.magnetic_trackers.values())

    corrupted = bytearray(datagram)
    corrupted[HEADER_STRUCT.size + 1] ^= 0x01
    try:
        decode_apm1_packet(corrupted)
    except APM1CRCError:
        pass
    else:  # pragma: no cover - this would indicate a broken checksum validator.
        raise AssertionError("corrupted packet unexpectedly passed CRC validation")

    print(
        f"[SELF-TEST] PASS: decoded {len(datagram)}-byte packet with "
        f"legacy APM1 plus {len(decoded_apm2.magnetic_samples)} side-labelled APM2 samples; "
        "CRC rejection verified"
    )
    return 0


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Receive APM1/APM2 pose/magnetic UDP packets, save pose.csv plus "
            "magnetic_right.csv and magnetic_left.csv, and optionally register "
            "with the iPhone gateway."
        )
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="Local address to bind")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_COMBINED_PORT,
        help=f"Combined-data UDP listen port (default: {DEFAULT_COMBINED_PORT})",
    )
    parser.add_argument(
        "--phone-ip",
        "--gateway-ip",
        dest="phone_ip",
        default=None,
        help="iPhone hotspot gateway address; omit to listen without PC_HELLO",
    )
    parser.add_argument(
        "--registration-port",
        type=int,
        default=DEFAULT_REGISTRATION_PORT,
        help=f"Phone registration UDP port (default: {DEFAULT_REGISTRATION_PORT})",
    )
    parser.add_argument(
        "--video-port",
        type=int,
        default=DEFAULT_VIDEO_PORT,
        help=f"Video port advertised in PC_HELLO (default: {DEFAULT_VIDEO_PORT})",
    )
    parser.add_argument(
        "--hello-interval",
        type=float,
        default=2.0,
        help="Seconds between PC_HELLO registrations (default: 2)",
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=1.0,
        help="Seconds between console statistics (default: 1)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="CSV directory (default: logs/pose_magnetic_<timestamp>)",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Build and decode a local test packet, then exit",
    )
    return parser


def _valid_port(parser: argparse.ArgumentParser, name: str, value: int) -> None:
    if not 1 <= value <= 65535:
        parser.error(f"{name} must be between 1 and 65535")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return run_self_test()

    _valid_port(parser, "--port", args.port)
    _valid_port(parser, "--registration-port", args.registration_port)
    _valid_port(parser, "--video-port", args.video_port)
    if args.hello_interval <= 0:
        parser.error("--hello-interval must be greater than zero")
    if args.status_interval <= 0:
        parser.error("--status-interval must be greater than zero")

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)
    return run_receiver(args)


if __name__ == "__main__":
    raise SystemExit(main())
