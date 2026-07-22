from __future__ import annotations

import argparse
import json
import math
import socket
import struct
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from select import select

try:
    import av
except Exception as exc:  # pragma: no cover - import guard for runtime only
    av = None
    AV_IMPORT_ERROR = exc
else:
    AV_IMPORT_ERROR = None

import numpy as np
try:
    import pyqtgraph as pg
except Exception:  # pragma: no cover - optional UI enhancement fallback
    pg = None
from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QPlainTextEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from aruco_gripper_tracker import (
    CameraIntrinsics as TrackingCameraIntrinsics,
    GripperDistanceProcessor,
    TrackerConfig,
)


POSE_PACKET = struct.Struct("<Id7f")
VIDEO_PACKET_HEADER_V1 = struct.Struct("<4sBBHIdHHHH")
VIDEO_PACKET_HEADER_V2 = struct.Struct("<4sBBHIdHHHHffffHH")
VIDEO_MAGIC_V1 = b"APV1"
VIDEO_MAGIC_V2 = b"APV2"
FRAME_STALE_SECONDS = 0.20
MAX_INFLIGHT_FRAMES = 8
LATENCY_HISTORY_SECONDS = 30.0
CLOCK_SAMPLE_WINDOW_SECONDS = 60.0
MAX_CLOCK_DELTA_SECONDS = 24.0 * 60.0 * 60.0


def build_av_install_hint() -> str:
    executable = Path(sys.executable).resolve()
    return f"\"{executable}\" -m pip install av"


def get_app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


@dataclass
class NALAssembly:
    total_fragments: int
    fragments: dict[int, bytes] = field(default_factory=dict)

    def is_complete(self) -> bool:
        return len(self.fragments) == self.total_fragments


@dataclass(frozen=True)
class CameraIntrinsics:
    fx: float
    fy: float
    cx: float
    cy: float
    image_width: int
    image_height: int

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in (self.fx, self.fy, self.cx, self.cy)):
            raise ValueError("Camera intrinsics contain a non-finite value")
        if self.fx <= 0.0 or self.fy <= 0.0 or self.image_width <= 0 or self.image_height <= 0:
            raise ValueError("Camera focal lengths and calibration resolution must be positive")


@dataclass(frozen=True)
class VideoPacketFragment:
    flags: int
    frame_id: int
    capture_timestamp: float
    nalu_index: int
    nalu_count: int
    fragment_index: int
    fragment_count: int
    payload: bytes
    camera_intrinsics: CameraIntrinsics | None


def decode_video_packet(packet: bytes) -> VideoPacketFragment:
    if len(packet) < 6:
        raise ValueError("Video packet is shorter than the magic/version prefix")

    magic = packet[:4]
    version = packet[4]
    camera_intrinsics = None

    if magic == VIDEO_MAGIC_V1 and version == 1:
        header = VIDEO_PACKET_HEADER_V1
        if len(packet) < header.size:
            raise ValueError("APV1 packet is shorter than its header")
        (
            _magic,
            _version,
            flags,
            _reserved,
            frame_id,
            capture_timestamp,
            nalu_index,
            nalu_count,
            fragment_index,
            fragment_count,
        ) = header.unpack_from(packet)
    elif magic == VIDEO_MAGIC_V2 and version == 2:
        header = VIDEO_PACKET_HEADER_V2
        if len(packet) < header.size:
            raise ValueError("APV2 packet is shorter than its header")
        (
            _magic,
            _version,
            flags,
            _reserved,
            frame_id,
            capture_timestamp,
            nalu_index,
            nalu_count,
            fragment_index,
            fragment_count,
            fx,
            fy,
            cx,
            cy,
            image_width,
            image_height,
        ) = header.unpack_from(packet)
        camera_intrinsics = CameraIntrinsics(
            fx=fx,
            fy=fy,
            cx=cx,
            cy=cy,
            image_width=image_width,
            image_height=image_height,
        )
    else:
        raise ValueError(f"Unsupported video protocol {magic!r} version {version}")

    if fragment_count <= 0 or nalu_count <= 0:
        raise ValueError("Video packet has an invalid fragment or NAL count")
    if not (0 <= nalu_index < nalu_count) or not (0 <= fragment_index < fragment_count):
        raise ValueError("Video packet index is outside the advertised range")

    return VideoPacketFragment(
        flags=flags,
        frame_id=frame_id,
        capture_timestamp=capture_timestamp,
        nalu_index=nalu_index,
        nalu_count=nalu_count,
        fragment_index=fragment_index,
        fragment_count=fragment_count,
        payload=packet[header.size :],
        camera_intrinsics=camera_intrinsics,
    )


@dataclass
class FrameAssembly:
    frame_id: int
    capture_timestamp: float
    nalu_count: int
    is_keyframe: bool
    created_at: float
    last_update_at: float
    first_received_wall_time: float
    camera_intrinsics: CameraIntrinsics | None = None
    nalus: dict[int, NALAssembly] = field(default_factory=dict)

    def is_complete(self) -> bool:
        if len(self.nalus) != self.nalu_count:
            return False
        return all(nalu.is_complete() for nalu in self.nalus.values())

    def to_annexb(self) -> bytes:
        chunks: list[bytes] = []
        for nalu_index in range(self.nalu_count):
            assembly = self.nalus.get(nalu_index)
            if assembly is None or not assembly.is_complete():
                raise ValueError(f"Frame {self.frame_id} is incomplete")
            payload = b"".join(assembly.fragments[index] for index in range(assembly.total_fragments))
            chunks.append(b"\x00\x00\x00\x01" + payload)
        return b"".join(chunks)


class LatencyClockCompensator:
    """Remove sender/receiver wall-clock offset from one-way latency samples.

    Pose packets are preferred as the low-overhead reference path. Video packet
    arrival is used only as a fallback when the pose receiver is disabled.
    """

    def __init__(self, sample_window_seconds: float = CLOCK_SAMPLE_WINDOW_SECONDS) -> None:
        self.sample_window_seconds = sample_window_seconds
        self._pose_samples: deque[tuple[float, float]] = deque()
        self._video_samples: deque[tuple[float, float]] = deque()

    def observe(
        self,
        sender_timestamp: float,
        receive_wall_time: float,
        receive_monotonic: float,
        *,
        is_pose_reference: bool,
    ) -> float | None:
        raw_delay = receive_wall_time - sender_timestamp
        if not self._is_valid_raw_delay(raw_delay):
            return None

        samples = self._pose_samples if is_pose_reference else self._video_samples
        samples.append((receive_monotonic, raw_delay))
        self._prune(receive_monotonic)
        return self.compensate_raw_delay(raw_delay)

    def compensate_raw_delay(self, raw_delay: float) -> float | None:
        offset = self.offset_seconds
        if offset is None or not self._is_valid_raw_delay(raw_delay):
            return None
        return max(0.0, (raw_delay - offset) * 1000.0)

    @property
    def offset_seconds(self) -> float | None:
        samples = self._pose_samples if self._pose_samples else self._video_samples
        if not samples:
            return None
        return min(raw_delay for _, raw_delay in samples)

    @property
    def reference_name(self) -> str:
        if self._pose_samples:
            return "Pose packets"
        if self._video_samples:
            return "Video fallback"
        return "Calibrating"

    def _prune(self, now_monotonic: float) -> None:
        cutoff = now_monotonic - self.sample_window_seconds
        for samples in (self._pose_samples, self._video_samples):
            while samples and samples[0][0] < cutoff:
                samples.popleft()

    @staticmethod
    def _is_valid_raw_delay(raw_delay: float) -> bool:
        return math.isfinite(raw_delay) and abs(raw_delay) <= MAX_CLOCK_DELTA_SECONDS


class VideoReceiverThread(QThread):
    frame_ready = pyqtSignal(QImage)
    video_metrics = pyqtSignal(dict)
    pose_metrics = pyqtSignal(dict)
    aruco_metrics = pyqtSignal(dict)
    status_changed = pyqtSignal(str)
    log_message = pyqtSignal(str)

    def __init__(
        self,
        bind_host: str,
        video_port: int,
        pose_port: int | None,
        aruco_config: TrackerConfig | None = None,
    ) -> None:
        super().__init__()
        self.bind_host = bind_host
        self.video_port = video_port
        self.pose_port = pose_port
        self._running = True
        self._decoder = self._create_decoder()
        self._frames: dict[int, FrameAssembly] = {}
        self._latest_decoded_frame_id: int | None = None
        self._waiting_for_keyframe = True
        self._latency_clock = LatencyClockCompensator()
        self._last_video_raw_delay: float | None = None
        self._last_pose_raw_delay: float | None = None
        self._video_byte_window: deque[tuple[float, int]] = deque()
        self._video_frame_window: deque[float] = deque()
        self._pose_packet_window: deque[float] = deque()
        self._video_state = {
            "status": "Idle",
            "frame_id": 0,
            "fps": 0.0,
            "bitrate_mbps": 0.0,
            "latency_ms": None,
            "raw_latency_ms": None,
            "capture_timestamp": None,
            "camera_intrinsics": None,
            "first_receive_wall_time": None,
            "decode_wall_time": None,
            "clock_offset_ms": None,
            "clock_reference": "Calibrating",
            "decoded_frames": 0,
            "dropped_frames": 0,
            "decode_errors": 0,
            "keyframes": 0,
            "packets": 0,
            "bytes": 0,
        }
        self._pose_state = {
            "status": "Pose idle",
            "sequence": 0,
            "latency_ms": None,
            "raw_latency_ms": None,
            "sender_timestamp": None,
            "receive_wall_time": None,
            "fps": 0.0,
            "drops": 0,
            "position": "(0.000, 0.000, 0.000)",
        }
        self._prev_pose_sequence: int | None = None
        self._aruco_processor = (
            GripperDistanceProcessor(aruco_config)
            if aruco_config is not None and aruco_config.tracking_enabled
            else None
        )
        self._aruco_output_address = (
            (aruco_config.output_host, aruco_config.output_port)
            if self._aruco_processor is not None and aruco_config is not None
            else None
        )
        self._aruco_output_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    @staticmethod
    def _create_decoder():
        if av is None:
            return None
        return av.CodecContext.create("h264", "r")

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        try:
            video_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if sys.platform != "win32":
                video_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            video_socket.bind((self.bind_host, self.video_port))
            video_socket.setblocking(False)

            pose_socket = None
            sockets = [video_socket]
            if self.pose_port is not None and self.pose_port > 0:
                pose_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                if sys.platform != "win32":
                    pose_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                pose_socket.bind((self.bind_host, self.pose_port))
                pose_socket.setblocking(False)
                sockets.append(pose_socket)
        except OSError as exc:
            self.status_changed.emit("Bind failed")
            self.log_message.emit(f"Could not bind UDP sockets: {exc}")
            self._aruco_output_socket.close()
            return

        self._video_state["status"] = f"Listening on {self.bind_host}:{self.video_port}"
        self.status_changed.emit(self._video_state["status"])
        self.video_metrics.emit(dict(self._video_state))
        self.pose_metrics.emit(dict(self._pose_state))
        self.log_message.emit(
            f"Listening for H.264 video on {self.bind_host}:{self.video_port}"
            + (f" and pose on {self.bind_host}:{self.pose_port}" if pose_socket else "")
        )

        try:
            while self._running:
                readable, _, _ = select(sockets, [], [], 0.1)
                now = time.monotonic()

                for current_socket in readable:
                    try:
                        packet, address = current_socket.recvfrom(65535)
                    except OSError as exc:
                        self.log_message.emit(f"Socket receive error: {exc}")
                        continue

                    if current_socket is video_socket:
                        self._handle_video_packet(packet, address)
                    else:
                        self._handle_pose_packet(packet, address)

                self._prune_stale_frames(now)
                self._emit_video_metrics()
                self._emit_pose_metrics()
        finally:
            video_socket.close()
            if pose_socket is not None:
                pose_socket.close()
            self._aruco_output_socket.close()
            self._video_state.update({"status": "Stopped", "fps": 0.0, "bitrate_mbps": 0.0})
            self._pose_state.update({"status": "Pose stopped", "fps": 0.0})
            self.video_metrics.emit(dict(self._video_state))
            self.pose_metrics.emit(dict(self._pose_state))
            self.status_changed.emit("Stopped")
            self.log_message.emit("Receiver stopped")

    def _handle_pose_packet(self, packet: bytes, address: tuple[str, int]) -> None:
        try:
            sequence, sender_time, x, y, z, *_quaternion = POSE_PACKET.unpack(packet)
        except struct.error:
            return

        recv_time = time.time()
        monotonic_now = time.monotonic()
        self._last_pose_raw_delay = recv_time - sender_time
        latency_ms = self._latency_clock.observe(
            sender_time,
            recv_time,
            monotonic_now,
            is_pose_reference=True,
        )
        self._pose_packet_window.append(monotonic_now)

        dropped = 0
        if self._prev_pose_sequence is not None:
            dropped = max(0, sequence - self._prev_pose_sequence - 1)
        self._prev_pose_sequence = sequence

        self._pose_state.update(
            {
                "status": f"Pose from {address[0]}:{address[1]}",
                "sequence": sequence,
                "latency_ms": latency_ms,
                "raw_latency_ms": self._last_pose_raw_delay * 1000.0,
                "sender_timestamp": sender_time,
                "receive_wall_time": recv_time,
                "drops": self._pose_state["drops"] + dropped,
                "position": f"({x:+.3f}, {y:+.3f}, {z:+.3f})",
            }
        )
        self._emit_pose_metrics()

    def _handle_video_packet(self, packet: bytes, address: tuple[str, int]) -> None:
        try:
            fragment = decode_video_packet(packet)
        except (ValueError, struct.error):
            return

        frame_id = fragment.frame_id
        capture_timestamp = fragment.capture_timestamp

        if self._latest_decoded_frame_id is not None and frame_id <= self._latest_decoded_frame_id:
            return

        now = time.monotonic()
        now_wall_clock = time.time()
        self._video_state["status"] = f"Receiving from {address[0]}:{address[1]}"
        self._video_state["packets"] += 1
        self._video_state["bytes"] += len(packet)
        self._video_byte_window.append((now, len(packet)))

        frame = self._frames.get(frame_id)
        if frame is None:
            self._latency_clock.observe(
                capture_timestamp,
                now_wall_clock,
                now,
                is_pose_reference=False,
            )
            frame = FrameAssembly(
                frame_id=frame_id,
                capture_timestamp=capture_timestamp,
                nalu_count=fragment.nalu_count,
                is_keyframe=bool(fragment.flags & 0x01),
                created_at=now,
                last_update_at=now,
                first_received_wall_time=now_wall_clock,
                camera_intrinsics=fragment.camera_intrinsics,
            )
            self._frames[frame_id] = frame
        else:
            frame.last_update_at = now
            if frame.camera_intrinsics is None:
                frame.camera_intrinsics = fragment.camera_intrinsics

        nalu = frame.nalus.get(fragment.nalu_index)
        if nalu is None:
            nalu = NALAssembly(total_fragments=fragment.fragment_count)
            frame.nalus[fragment.nalu_index] = nalu
        elif nalu.total_fragments != fragment.fragment_count:
            nalu.total_fragments = max(nalu.total_fragments, fragment.fragment_count)

        if fragment.fragment_index not in nalu.fragments:
            nalu.fragments[fragment.fragment_index] = fragment.payload

        if frame.is_complete():
            self._frames.pop(frame_id, None)
            self._decode_frame(frame)

        self._trim_inflight_frames()

    def _trim_inflight_frames(self) -> None:
        if len(self._frames) <= MAX_INFLIGHT_FRAMES:
            return

        for frame_id, _frame in sorted(self._frames.items(), key=lambda item: item[1].created_at)[:-MAX_INFLIGHT_FRAMES]:
            self._frames.pop(frame_id, None)
            self._video_state["dropped_frames"] += 1

    def _prune_stale_frames(self, now: float) -> None:
        stale_ids = [
            frame_id
            for frame_id, frame in self._frames.items()
            if now - frame.last_update_at >= FRAME_STALE_SECONDS
        ]
        for frame_id in stale_ids:
            self._frames.pop(frame_id, None)
            self._video_state["dropped_frames"] += 1

    def _decode_frame(self, frame: FrameAssembly) -> None:
        if av is None or self._decoder is None:
            return

        if self._waiting_for_keyframe and not frame.is_keyframe:
            self._video_state["status"] = "Waiting for keyframe"
            return

        try:
            annexb = frame.to_annexb()
            if frame.is_keyframe:
                self._decoder = self._create_decoder()
                self._waiting_for_keyframe = False

            decoded_frames = self._decode_annexb_packet(annexb)
            decoded_any = False
            for decoded_frame in decoded_frames:
                decoded_any = True
                self._handle_decoded_frame(decoded_frame, frame)

            if not decoded_any:
                if frame.is_keyframe:
                    self._waiting_for_keyframe = True
                    self._video_state["status"] = "Waiting for decoded keyframe"
                return

            self._waiting_for_keyframe = False
            self._video_state["status"] = "Video decoding"
        except Exception as exc:
            self._video_state["decode_errors"] += 1
            self._video_state["status"] = "Decode error"
            self._waiting_for_keyframe = True
            self._decoder = self._create_decoder()
            self.log_message.emit(f"Decode error on frame {frame.frame_id}: {exc}")

    def _decode_annexb_packet(self, annexb: bytes):
        if self._decoder is None:
            return []

        packet = av.Packet(annexb)
        decoded_frames = self._decoder.decode(packet)
        if decoded_frames:
            return decoded_frames

        fallback_frames = []
        for parsed_packet in self._decoder.parse(annexb):
            fallback_frames.extend(self._decoder.decode(parsed_packet))
        return fallback_frames

    def _handle_decoded_frame(self, decoded_frame: av.VideoFrame, frame: FrameAssembly) -> None:
        rgb = decoded_frame.to_ndarray(format="rgb24")
        height, width, channels = rgb.shape
        image = QImage(
            rgb.data,
            width,
            height,
            channels * width,
            QImage.Format.Format_RGB888,
        ).copy()

        now_monotonic = time.monotonic()
        now_wall_clock = time.time()
        self._last_video_raw_delay = now_wall_clock - frame.capture_timestamp
        self._video_frame_window.append(now_monotonic)
        self._latest_decoded_frame_id = frame.frame_id
        self._video_state["frame_id"] = frame.frame_id
        self._video_state["decoded_frames"] += 1
        self._video_state["latency_ms"] = self._latency_clock.compensate_raw_delay(
            self._last_video_raw_delay
        )
        self._video_state["raw_latency_ms"] = self._last_video_raw_delay * 1000.0
        self._video_state["capture_timestamp"] = frame.capture_timestamp
        self._video_state["camera_intrinsics"] = frame.camera_intrinsics
        self._video_state["first_receive_wall_time"] = frame.first_received_wall_time
        self._video_state["decode_wall_time"] = now_wall_clock
        if frame.is_keyframe:
            self._video_state["keyframes"] += 1

        if self._aruco_processor is not None:
            try:
                camera_intrinsics = None
                if frame.camera_intrinsics is not None:
                    camera_intrinsics = TrackingCameraIntrinsics(
                        fx=frame.camera_intrinsics.fx,
                        fy=frame.camera_intrinsics.fy,
                        cx=frame.camera_intrinsics.cx,
                        cy=frame.camera_intrinsics.cy,
                        image_width=frame.camera_intrinsics.image_width,
                        image_height=frame.camera_intrinsics.image_height,
                    )
                result = self._aruco_processor.process(
                    image_bgr=np.ascontiguousarray(rgb[:, :, ::-1]),
                    frame_id=frame.frame_id,
                    capture_timestamp=frame.capture_timestamp,
                    camera_intrinsics=camera_intrinsics,
                )
                if self._aruco_output_address is not None:
                    payload = json.dumps(
                        result,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode("utf-8")
                    try:
                        self._aruco_output_socket.sendto(payload, self._aruco_output_address)
                    except OSError as exc:
                        result["output_error"] = str(exc)
                self.aruco_metrics.emit(result)
            except Exception as exc:
                self.aruco_metrics.emit(
                    {
                        "protocol": "AGP1",
                        "frame_id": frame.frame_id,
                        "capture_time": frame.capture_timestamp,
                        "status": "processor_error",
                        "detected_ids": [],
                        "markers": {},
                        "gripper_distance": None,
                        "error": str(exc),
                    }
                )

        self._emit_video_metrics()
        self.frame_ready.emit(image)

    def _emit_video_metrics(self) -> None:
        now = time.monotonic()
        while self._video_frame_window and now - self._video_frame_window[0] > 1.0:
            self._video_frame_window.popleft()
        while self._video_byte_window and now - self._video_byte_window[0][0] > 1.0:
            self._video_byte_window.popleft()

        self._video_state["fps"] = float(len(self._video_frame_window))
        bytes_last_second = sum(size for _, size in self._video_byte_window)
        self._video_state["bitrate_mbps"] = (bytes_last_second * 8.0) / 1_000_000.0
        if self._last_video_raw_delay is not None:
            self._video_state["latency_ms"] = self._latency_clock.compensate_raw_delay(
                self._last_video_raw_delay
            )
        offset_seconds = self._latency_clock.offset_seconds
        self._video_state["clock_offset_ms"] = (
            offset_seconds * 1000.0 if offset_seconds is not None else None
        )
        self._video_state["clock_reference"] = self._latency_clock.reference_name
        self.video_metrics.emit(dict(self._video_state))

    def _emit_pose_metrics(self) -> None:
        now = time.monotonic()
        while self._pose_packet_window and now - self._pose_packet_window[0] > 1.0:
            self._pose_packet_window.popleft()

        self._pose_state["fps"] = float(len(self._pose_packet_window))
        if self._last_pose_raw_delay is not None:
            self._pose_state["latency_ms"] = self._latency_clock.compensate_raw_delay(
                self._last_pose_raw_delay
            )
        self.pose_metrics.emit(dict(self._pose_state))


class LabeledValue(QLabel):
    def __init__(self, text: str = "--") -> None:
        super().__init__(text)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setFont(QFont("Consolas", 10))


class VideoDebugWindow(QMainWindow):
    def __init__(self, bind_host: str, video_port: int, pose_port: int | None) -> None:
        super().__init__()
        self.worker: VideoReceiverThread | None = None
        self.video_latency_history: deque[tuple[float, float]] = deque()
        self.pose_latency_history: deque[tuple[float, float]] = deque()
        self.last_video_frame_id = -1
        self.last_pose_sequence = -1
        self.setWindowTitle("ARPose Low-Latency Video Debugger")
        self.resize(1320, 860)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        controls_box = QGroupBox("Receiver")
        controls_layout = QGridLayout(controls_box)
        controls_layout.setHorizontalSpacing(12)
        controls_layout.setVerticalSpacing(10)
        self.bind_host_edit = QLineEdit(bind_host)
        self.video_port_edit = QLineEdit(str(video_port))
        self.pose_port_edit = QLineEdit("" if pose_port is None else str(pose_port))
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.status_value = LabeledValue("Idle")

        controls_layout.addWidget(QLabel("Bind Host"), 0, 0)
        controls_layout.addWidget(self.bind_host_edit, 0, 1)
        controls_layout.addWidget(QLabel("Video Port"), 0, 2)
        controls_layout.addWidget(self.video_port_edit, 0, 3)
        controls_layout.addWidget(QLabel("Pose Port"), 0, 4)
        controls_layout.addWidget(self.pose_port_edit, 0, 5)
        controls_layout.addWidget(self.start_button, 0, 6)
        controls_layout.addWidget(self.stop_button, 0, 7)
        controls_layout.addWidget(QLabel("Status"), 1, 0)
        controls_layout.addWidget(self.status_value, 1, 1, 1, 7)

        body = QHBoxLayout()
        body.setSpacing(14)

        preview_column = QVBoxLayout()
        preview_column.setSpacing(14)

        preview_box = QGroupBox("Live Preview")
        preview_layout = QVBoxLayout(preview_box)
        self.preview_label = QLabel("No video frames yet")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(860, 440)
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.preview_label.setStyleSheet(
            "background-color: #0f1720; border: 1px solid #223040; border-radius: 14px; color: #93a9be;"
        )
        preview_layout.addWidget(self.preview_label)

        latency_box = QGroupBox("Latency Curve")
        latency_layout = QVBoxLayout(latency_box)
        if pg is not None:
            self.latency_plot = pg.PlotWidget()
            self.latency_plot.setBackground("#111b24")
            self.latency_plot.showGrid(x=True, y=True, alpha=0.18)
            self.latency_plot.setMenuEnabled(False)
            self.latency_plot.setMouseEnabled(x=False, y=False)
            self.latency_plot.setYRange(0, 100, padding=0.05)
            self.latency_plot.setXRange(-LATENCY_HISTORY_SECONDS, 0, padding=0.0)
            self.latency_plot.setLabel("left", "Latency", units="ms")
            self.latency_plot.setLabel("bottom", "Seconds Ago")
            self.latency_plot.addLegend(offset=(12, 12))
            self.latency_plot.getPlotItem().setClipToView(True)
            self.latency_plot.getAxis("left").setTextPen("#9fb3c5")
            self.latency_plot.getAxis("bottom").setTextPen("#9fb3c5")
            self.latency_plot.getAxis("left").setPen(pg.mkPen("#38536a"))
            self.latency_plot.getAxis("bottom").setPen(pg.mkPen("#38536a"))
            self.latency_plot.getPlotItem().vb.setLimits(xMin=-LATENCY_HISTORY_SECONDS, xMax=0)
            self.latency_video_curve = self.latency_plot.plot(
                [],
                [],
                pen=pg.mkPen("#4db7ff", width=2),
                name="Video",
            )
            self.latency_pose_curve = self.latency_plot.plot(
                [],
                [],
                pen=pg.mkPen("#ffb14d", width=2),
                name="Pose",
            )
            latency_layout.addWidget(self.latency_plot)
        else:
            self.latency_plot = None
            self.latency_video_curve = None
            self.latency_pose_curve = None
            self.latency_plot_unavailable = QLabel("Latency curve needs pyqtgraph installed.")
            self.latency_plot_unavailable.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.latency_plot_unavailable.setMinimumHeight(220)
            self.latency_plot_unavailable.setStyleSheet(
                "background-color: #111b24; border: 1px solid #223040; border-radius: 14px; color: #93a9be;"
            )
            latency_layout.addWidget(self.latency_plot_unavailable)

        sidebar = QVBoxLayout()
        sidebar.setSpacing(14)

        video_box = QGroupBox("Video Metrics")
        video_form = QFormLayout(video_box)
        self.video_state_value = LabeledValue("Idle")
        self.video_frame_id_value = LabeledValue("0")
        self.video_fps_value = LabeledValue("0.0")
        self.video_bitrate_value = LabeledValue("0.0")
        self.video_latency_value = LabeledValue("--")
        self.video_clock_offset_value = LabeledValue("--")
        self.video_clock_reference_value = LabeledValue("Calibrating")
        self.video_decoded_value = LabeledValue("0")
        self.video_dropped_value = LabeledValue("0")
        self.video_keyframes_value = LabeledValue("0")
        self.video_packets_value = LabeledValue("0")
        self.video_bytes_value = LabeledValue("0 B")
        for label, widget in [
            ("State", self.video_state_value),
            ("Frame ID", self.video_frame_id_value),
            ("Decoded FPS", self.video_fps_value),
            ("Bitrate", self.video_bitrate_value),
            ("Est. Latency", self.video_latency_value),
            ("Clock Offset", self.video_clock_offset_value),
            ("Clock Reference", self.video_clock_reference_value),
            ("Decoded Frames", self.video_decoded_value),
            ("Dropped Frames", self.video_dropped_value),
            ("Keyframes", self.video_keyframes_value),
            ("Packets", self.video_packets_value),
            ("Bytes", self.video_bytes_value),
        ]:
            video_form.addRow(label, widget)
        self.video_latency_value.setToolTip(
            "Capture-to-display latency after removing the estimated phone/PC clock offset."
        )
        self.video_clock_offset_value.setToolTip(
            "Estimated phone-to-PC wall-clock offset; pose packets are used when available."
        )

        pose_box = QGroupBox("Pose Feed")
        pose_form = QFormLayout(pose_box)
        self.pose_state_value = LabeledValue("Pose idle")
        self.pose_sequence_value = LabeledValue("0")
        self.pose_fps_value = LabeledValue("0.0")
        self.pose_latency_value = LabeledValue("--")
        self.pose_drop_value = LabeledValue("0")
        self.pose_position_value = LabeledValue("(0.000, 0.000, 0.000)")
        for label, widget in [
            ("State", self.pose_state_value),
            ("Sequence", self.pose_sequence_value),
            ("FPS", self.pose_fps_value),
            ("Est. Latency", self.pose_latency_value),
            ("Drops", self.pose_drop_value),
            ("Position", self.pose_position_value),
        ]:
            pose_form.addRow(label, widget)

        log_box = QGroupBox("Runtime Log")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(500)
        log_layout.addWidget(self.log_view)

        sidebar.addWidget(video_box)
        sidebar.addWidget(pose_box)
        sidebar.addWidget(log_box, 1)

        preview_column.addWidget(preview_box, 3)
        preview_column.addWidget(latency_box, 2)

        body.addLayout(preview_column, 2)
        body.addLayout(sidebar, 1)

        root.addWidget(controls_box)
        root.addLayout(body, 1)

        self.start_button.clicked.connect(self.start_receiver)
        self.stop_button.clicked.connect(self.stop_receiver)
        self._apply_theme()

    def _apply_theme(self) -> None:
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#0b1218"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#e8f0f6"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#111b24"))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#16232f"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#ecf3f8"))
        palette.setColor(QPalette.ColorRole.Button, QColor("#193244"))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#f5fbff"))
        self.setPalette(palette)
        self.setStyleSheet(
            """
            QWidget {
                background-color: #0b1218;
                color: #e8f0f6;
                font-size: 13px;
            }
            QGroupBox {
                border: 1px solid #223040;
                border-radius: 14px;
                margin-top: 10px;
                padding-top: 14px;
                font-weight: 600;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
                color: #cfe2f0;
            }
            QLineEdit, QPlainTextEdit {
                background-color: #111b24;
                border: 1px solid #2c455a;
                border-radius: 10px;
                padding: 8px 10px;
                selection-background-color: #2a7db4;
            }
            QPushButton {
                background-color: #1f6d9c;
                border: none;
                border-radius: 10px;
                padding: 9px 16px;
                font-weight: 600;
            }
            QPushButton:disabled {
                background-color: #324452;
                color: #9eb0bd;
            }
            QPushButton:hover:!disabled {
                background-color: #2582ba;
            }
            """
        )

    def start_receiver(self) -> None:
        if av is None:
            self.append_log(f"PyAV import failed: {AV_IMPORT_ERROR}")
            self.append_log(f"Active interpreter: {sys.executable}")
            self.append_log(f"Install it with: {build_av_install_hint()}")
            return

        bind_host = self.bind_host_edit.text().strip() or "0.0.0.0"
        try:
            video_port = int(self.video_port_edit.text().strip())
            pose_port_text = self.pose_port_edit.text().strip()
            pose_port = int(pose_port_text) if pose_port_text else None
        except ValueError:
            self.append_log("Ports must be integers.")
            return

        self.worker = VideoReceiverThread(bind_host=bind_host, video_port=video_port, pose_port=pose_port)
        self.worker.frame_ready.connect(self.update_preview)
        self.worker.video_metrics.connect(self.update_video_metrics)
        self.worker.pose_metrics.connect(self.update_pose_metrics)
        self.worker.status_changed.connect(self.status_value.setText)
        self.worker.log_message.connect(self.append_log)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()
        self.reset_latency_history()

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.append_log(f"Starting receiver on {bind_host}:{video_port}")

    def stop_receiver(self) -> None:
        if self.worker is None:
            return
        self.worker.stop()
        self.worker.wait(1500)

    def on_worker_finished(self) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.worker = None

    def update_preview(self, image: QImage) -> None:
        pixmap = QPixmap.fromImage(image)
        scaled = pixmap.scaled(
            self.preview_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview_label.setPixmap(scaled)

    def update_video_metrics(self, metrics: dict) -> None:
        self.video_state_value.setText(str(metrics["status"]))
        self.video_frame_id_value.setText(str(metrics["frame_id"]))
        self.video_fps_value.setText(f"{metrics['fps']:.1f}")
        self.video_bitrate_value.setText(f"{metrics['bitrate_mbps']:.2f} Mbps")
        latency_ms = metrics.get("latency_ms")
        self.video_latency_value.setText(self.format_latency(latency_ms))
        self.video_clock_offset_value.setText(
            self.format_latency(metrics.get("clock_offset_ms"), include_sign=True)
        )
        self.video_clock_reference_value.setText(str(metrics.get("clock_reference", "--")))
        self.video_decoded_value.setText(str(metrics["decoded_frames"]))
        self.video_dropped_value.setText(str(metrics["dropped_frames"]))
        self.video_keyframes_value.setText(str(metrics["keyframes"]))
        self.video_packets_value.setText(str(metrics["packets"]))
        self.video_bytes_value.setText(self.format_bytes(int(metrics["bytes"])))

        frame_id = int(metrics["frame_id"])
        if frame_id > self.last_video_frame_id:
            self.last_video_frame_id = frame_id
            if self.is_valid_metric(latency_ms):
                self.append_latency_sample(self.video_latency_history, float(latency_ms))

    def update_pose_metrics(self, metrics: dict) -> None:
        self.pose_state_value.setText(str(metrics["status"]))
        self.pose_sequence_value.setText(str(metrics["sequence"]))
        self.pose_fps_value.setText(f"{metrics['fps']:.1f}")
        latency_ms = metrics.get("latency_ms")
        self.pose_latency_value.setText(self.format_latency(latency_ms))
        self.pose_drop_value.setText(str(metrics["drops"]))
        self.pose_position_value.setText(str(metrics["position"]))

        sequence = int(metrics["sequence"])
        if sequence > self.last_pose_sequence:
            self.last_pose_sequence = sequence
            if self.is_valid_metric(latency_ms):
                self.append_latency_sample(self.pose_latency_history, float(latency_ms))

    def append_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{timestamp}] {message}")

    def closeEvent(self, event) -> None:  # noqa: N802
        self.stop_receiver()
        super().closeEvent(event)

    @staticmethod
    def format_bytes(num_bytes: int) -> str:
        units = ["B", "KB", "MB", "GB"]
        value = float(num_bytes)
        for unit in units:
            if value < 1024.0 or unit == units[-1]:
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
            value /= 1024.0
        return f"{num_bytes} B"

    @staticmethod
    def is_valid_metric(value: object) -> bool:
        return isinstance(value, (int, float)) and math.isfinite(float(value))

    @classmethod
    def format_latency(cls, value: object, *, include_sign: bool = False) -> str:
        if not cls.is_valid_metric(value):
            return "--"
        sign = "+" if include_sign else ""
        return f"{float(value):{sign}.1f} ms"

    def reset_latency_history(self) -> None:
        self.video_latency_history.clear()
        self.pose_latency_history.clear()
        self.last_video_frame_id = -1
        self.last_pose_sequence = -1
        self.refresh_latency_plot()

    def append_latency_sample(self, history: deque[tuple[float, float]], latency_ms: float) -> None:
        now = time.monotonic()
        history.append((now, latency_ms))
        cutoff = now - LATENCY_HISTORY_SECONDS
        while history and history[0][0] < cutoff:
            history.popleft()
        self.refresh_latency_plot()

    def refresh_latency_plot(self) -> None:
        if self.latency_plot is None or self.latency_video_curve is None or self.latency_pose_curve is None:
            return

        now = time.monotonic()
        cutoff = now - LATENCY_HISTORY_SECONDS

        while self.video_latency_history and self.video_latency_history[0][0] < cutoff:
            self.video_latency_history.popleft()
        while self.pose_latency_history and self.pose_latency_history[0][0] < cutoff:
            self.pose_latency_history.popleft()

        video_x = [timestamp - now for timestamp, _ in self.video_latency_history]
        video_y = [latency for _, latency in self.video_latency_history]
        pose_x = [timestamp - now for timestamp, _ in self.pose_latency_history]
        pose_y = [latency for _, latency in self.pose_latency_history]

        self.latency_video_curve.setData(video_x, video_y)
        self.latency_pose_curve.setData(pose_x, pose_y)

        max_latency = max(video_y + pose_y, default=100.0)
        upper_bound = max(50.0, min(max_latency * 1.25, 500.0))
        self.latency_plot.setXRange(-LATENCY_HISTORY_SECONDS, 0, padding=0.0)
        self.latency_plot.setYRange(0, upper_bound, padding=0.02)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Debug viewer for ARPose low-latency H.264 video over UDP.")
    parser.add_argument("--bind", default="0.0.0.0", help="Host/IP to bind to.")
    parser.add_argument("--video-port", type=int, default=5560, help="UDP video port to bind to.")
    parser.add_argument("--pose-port", type=int, default=5555, help="Optional pose UDP port to bind to. Use 0 to disable.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("ARPose Low-Latency Video Debugger")
    app.setOrganizationName("ARPoseStreamer")

    if av is None:
        print("PyAV is required for udp_video_debug_ui.py")
        print(f"Active interpreter: {sys.executable}")
        print(f"Import error: {AV_IMPORT_ERROR}")
        print(f"Install it with: {build_av_install_hint()}")

    pose_port = args.pose_port if args.pose_port > 0 else None
    window = VideoDebugWindow(bind_host=args.bind, video_port=args.video_port, pose_port=pose_port)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
