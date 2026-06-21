import argparse
import colorsys
import os
import platform
import socket
import struct
import subprocess
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import numpy as np
import pyqtgraph as pg
import pyqtgraph.opengl as gl
from PyQt6.QtCore import QThread, QTimer, pyqtSignal, Qt
from PyQt6.QtGui import QFont, QClipboard
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QRadioButton, QButtonGroup, QLineEdit, QGroupBox, QCheckBox, QProgressBar
)

# UDP packet format (from udp_pose_receiver.py)
FLOAT32_PACKET = struct.Struct("<Id7f")
UPLOAD_CHUNK_SIZE = 64 * 1024


def get_app_base_dir():
    """Resolve the directory that should own runtime data."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_default_upload_dir():
    """Return the default upload folder used by the desktop tools."""
    return (get_app_base_dir() / "uploads").resolve()


def format_bytes(num_bytes):
    """Format a byte count for compact UI display."""
    value = float(num_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0


def get_local_ip():
    """Get local IP address"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def decode_packet(packet: bytes, encoding: str):
    """Decode UDP packet (reused from udp_pose_receiver.py)"""
    if encoding == "binary":
        if len(packet) != FLOAT32_PACKET.size:
            raise ValueError(f"Expected {FLOAT32_PACKET.size} bytes, got {len(packet)}")
        return FLOAT32_PACKET.unpack(packet)

    text = packet.decode("utf-8").strip()
    fields = text.split(",")
    if len(fields) != 9:
        raise ValueError(f"Expected 9 CSV values, got {len(fields)}")

    sequence = int(fields[0])
    values = [float(x) for x in fields[1:]]
    return (sequence, *values)


class UploadHandler(BaseHTTPRequestHandler):
    """HTTP upload handler for receiving files from iPhone"""
    upload_root = get_default_upload_dir()
    file_received_callback = None
    progress_callback = None

    @classmethod
    def report_progress(cls, event):
        if cls.progress_callback:
            cls.progress_callback(event)

    def do_POST(self):
        if self.path != "/upload":
            self.send_error(404, "Unknown endpoint")
            return

        capture_id = self.headers.get("X-Capture-ID")
        component = self.headers.get("X-Capture-Component")
        original_filename = self.headers.get("X-Original-Filename")
        upload_kind = self.headers.get("X-Upload-Kind")
        content_length = self.headers.get("Content-Length")

        if not capture_id or not component or not original_filename or not upload_kind or not content_length:
            self.send_error(400, "Missing required headers")
            return

        try:
            body_length = int(content_length)
        except ValueError:
            self.send_error(400, "Invalid Content-Length")
            return
        if body_length <= 0:
            self.send_error(400, "Empty upload body")
            return

        safe_capture_id = capture_id.replace("/", "_").replace("\\", "_")
        safe_filename = Path(original_filename).name
        target_dir = self.upload_root / safe_capture_id
        target_dir.mkdir(parents=True, exist_ok=True)

        file_path = target_dir / f"{component}__{safe_filename}"
        temp_path = file_path.with_name(f"{file_path.name}.part")
        upload_kind_label = str(upload_kind)
        self.report_progress({
            "stage": "starting",
            "capture_id": safe_capture_id,
            "filename": safe_filename,
            "component": component,
            "upload_kind": upload_kind_label,
            "bytes_received": 0,
            "total_bytes": body_length,
            "percent": 0,
            "target_dir": str(target_dir.resolve()),
            "saved_to": str(file_path.resolve()),
        })

        bytes_received = 0
        last_percent = -1
        with temp_path.open("wb") as handle:
            while bytes_received < body_length:
                remaining = body_length - bytes_received
                chunk = self.rfile.read(min(UPLOAD_CHUNK_SIZE, remaining))
                if not chunk:
                    break
                handle.write(chunk)
                bytes_received += len(chunk)
                percent = int((bytes_received / body_length) * 100) if body_length else 100
                if percent != last_percent:
                    self.report_progress({
                        "stage": "receiving",
                        "capture_id": safe_capture_id,
                        "filename": safe_filename,
                        "component": component,
                        "upload_kind": upload_kind_label,
                        "bytes_received": bytes_received,
                        "total_bytes": body_length,
                        "percent": percent,
                        "target_dir": str(target_dir.resolve()),
                        "saved_to": str(file_path.resolve()),
                    })
                    last_percent = percent

        if bytes_received != body_length:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.report_progress({
                "stage": "error",
                "capture_id": safe_capture_id,
                "filename": safe_filename,
                "component": component,
                "upload_kind": upload_kind_label,
                "bytes_received": bytes_received,
                "total_bytes": body_length,
                "percent": int((bytes_received / body_length) * 100) if body_length else 0,
                "target_dir": str(target_dir.resolve()),
                "saved_to": str(file_path.resolve()),
                "message": f"Upload truncated: expected {body_length} bytes, received {bytes_received}",
            })
            self.send_error(400, "Incomplete upload body")
            return

        temp_path.replace(file_path)

        # Notify callback
        if self.file_received_callback:
            self.file_received_callback(safe_capture_id, safe_filename, str(file_path.resolve()))

        self.report_progress({
            "stage": "completed",
            "capture_id": safe_capture_id,
            "filename": safe_filename,
            "component": component,
            "upload_kind": upload_kind_label,
            "bytes_received": body_length,
            "total_bytes": body_length,
            "percent": 100,
            "target_dir": str(target_dir.resolve()),
            "saved_to": str(file_path.resolve()),
        })

        response = {
            "ok": True,
            "capture_id": safe_capture_id,
            "component": component,
            "upload_kind": upload_kind,
            "saved_to": str(file_path.resolve()),
        }

        import json
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):
        # Suppress log messages
        pass


class UploadServerThread(QThread):
    """Background thread for HTTP upload server"""
    server_started = pyqtSignal(str)  # Emits server URL
    file_received = pyqtSignal(str, str, str)  # Emits (capture_id, filename, saved_to)
    upload_progress = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)

    def __init__(self, port=8000, upload_dir=None):
        super().__init__()
        self.port = port
        self.upload_dir = Path(upload_dir) if upload_dir is not None else get_default_upload_dir()
        self.server = None
        self.running = False

    def run(self):
        try:
            UploadHandler.upload_root = self.upload_dir
            UploadHandler.file_received_callback = self.on_file_received
            UploadHandler.progress_callback = self.on_upload_progress
            self.upload_dir.mkdir(parents=True, exist_ok=True)

            self.server = HTTPServer(('0.0.0.0', self.port), UploadHandler)
            self.server.timeout = 0.5  # Non-blocking with timeout
            self.running = True

            local_ip = get_local_ip()
            self.server_started.emit(f"http://{local_ip}:{self.port}")

            while self.running:
                self.server.handle_request()
        except Exception as e:
            self.error_occurred.emit(str(e))

    def on_file_received(self, capture_id, filename, saved_to):
        self.file_received.emit(capture_id, filename, saved_to)

    def on_upload_progress(self, event):
        self.upload_progress.emit(event)

    def stop(self):
        self.running = False
        if self.server:
            try:
                self.server.server_close()
            except:
                pass


class RingBuffer:
    """Efficient ring buffer for trajectory data"""
    def __init__(self, max_size=10000):
        self.max_size = max_size
        self.positions = np.zeros((max_size, 3))
        self.timestamps = np.zeros(max_size)
        self.head = 0
        self.size = 0

    def append(self, position, timestamp):
        self.positions[self.head] = position
        self.timestamps[self.head] = timestamp
        self.head = (self.head + 1) % self.max_size
        self.size = min(self.size + 1, self.max_size)

    def get_all(self):
        if self.size == 0:
            return np.array([]), np.array([])
        if self.size < self.max_size:
            return self.positions[:self.size].copy(), self.timestamps[:self.size].copy()
        # Reorder to chronological
        indices = np.arange(self.head, self.head + self.max_size) % self.max_size
        return self.positions[indices].copy(), self.timestamps[indices].copy()


class CoordinateSystem:
    """Manage relative coordinate transformation"""
    def __init__(self):
        self.origin = None

    def reset_origin(self, position):
        self.origin = np.array(position)

    def to_relative(self, position):
        if self.origin is None:
            self.origin = np.array(position)
        return np.array(position) - self.origin


class TrajectoryManager:
    """Manage trajectory data and color computation"""
    def __init__(self):
        self.buffer = RingBuffer(max_size=10000)
        self.coord_system = CoordinateSystem()
        self.show_all = False  # False = last 5s, True = all history

    def add_point(self, position, timestamp):
        rel_pos = self.coord_system.to_relative(position)
        self.buffer.append(rel_pos, timestamp)

    def reset_origin(self, position):
        self.coord_system.reset_origin(position)

    def get_trajectory(self):
        positions, timestamps = self.buffer.get_all()
        if len(positions) == 0:
            return positions, timestamps, np.array([])

        # Filter by time window if needed
        if not self.show_all:
            current_time = time.time()
            cutoff = current_time - 5.0
            mask = timestamps >= cutoff
            positions = positions[mask]
            timestamps = timestamps[mask]

        # Compute gradient colors
        colors = self.compute_gradient_colors(timestamps)
        return positions, timestamps, colors

    def compute_gradient_colors(self, timestamps):
        if len(timestamps) < 2:
            return np.array([[0, 1, 1, 1]])  # Cyan

        # Normalize time [0, 1]
        t_min, t_max = timestamps.min(), timestamps.max()
        normalized = (timestamps - t_min) / (t_max - t_min + 1e-9)

        # HSV: Cyan (180°) → Red (0°)
        hues = 180 - normalized * 180
        colors = np.array([colorsys.hsv_to_rgb(h / 360, 0.9, 1.0) + (1.0,) for h in hues])
        return colors


class UDPReceiverThread(QThread):
    """Background UDP receiver thread"""
    packet_received = pyqtSignal(dict)
    stats_updated = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)

    def __init__(self, host="0.0.0.0", port=5555, encoding="binary"):
        super().__init__()
        self.host = host
        self.port = port
        self.encoding = encoding
        self.running = False
        self.prev_sequence = None
        self.prev_recv_time = None
        self.packet_count = 0
        self.drop_count = 0
        self.start_time = None

    def run(self):
        self.running = True
        self.start_time = time.time()

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((self.host, self.port))
            sock.settimeout(0.1)

            while self.running:
                try:
                    packet, address = sock.recvfrom(4096)
                    recv_time = time.time()
                    monotonic_recv_time = time.monotonic()

                    sequence, sender_time, x, y, z, qx, qy, qz, qw = decode_packet(packet, self.encoding)

                    # Calculate stats
                    approx_latency_ms = max(0.0, (recv_time - sender_time) * 1000.0)

                    if self.prev_recv_time is None:
                        fps = 0.0
                    else:
                        fps = 1.0 / max(monotonic_recv_time - self.prev_recv_time, 1e-9)
                    self.prev_recv_time = monotonic_recv_time

                    dropped = 0 if self.prev_sequence is None else max(0, sequence - self.prev_sequence - 1)
                    self.prev_sequence = sequence
                    self.drop_count += dropped
                    self.packet_count += 1

                    # Emit signals
                    self.packet_received.emit({
                        'position': (x, y, z),
                        'orientation': (qx, qy, qz, qw),
                        'timestamp': recv_time,
                        'sequence': sequence
                    })

                    self.stats_updated.emit({
                        'fps': fps,
                        'latency_ms': approx_latency_ms,
                        'packet_count': self.packet_count,
                        'drop_count': self.drop_count,
                        'position': (x, y, z),
                        'address': address,
                        'uptime': recv_time - self.start_time
                    })

                except socket.timeout:
                    continue
                except Exception as e:
                    self.error_occurred.emit(str(e))

            sock.close()
        except Exception as e:
            self.error_occurred.emit(f"Socket error: {e}")

    def stop(self):
        self.running = False


class Visualizer3D(gl.GLViewWidget):
    """3D visualization widget"""
    def __init__(self):
        super().__init__()
        self.setBackgroundColor('#1a1a2e')

        # Add coordinate axes
        self.add_axes()

        # Add grid floor
        self.grid = gl.GLGridItem()
        self.grid.setColor((42, 42, 62, 100))
        self.addItem(self.grid)

        # Trajectory line
        self.trajectory_item = gl.GLLinePlotItem(
            pos=np.array([[0, 0, 0]]),
            color=(0, 1, 1, 1),
            width=3.0,
            antialias=True
        )
        self.addItem(self.trajectory_item)

        # Current position marker
        self.marker_item = gl.GLScatterPlotItem(
            pos=np.array([[0, 0, 0]]),
            color=(1, 0, 0, 1),
            size=10,
            pxMode=True
        )
        self.addItem(self.marker_item)

        # Camera setup
        self.setCameraPosition(distance=2.0, elevation=30, azimuth=45)

    def add_axes(self):
        # X axis (red)
        x_axis = gl.GLLinePlotItem(
            pos=np.array([[0, 0, 0], [0.5, 0, 0]]),
            color=(1, 0, 0, 1),
            width=2.0
        )
        self.addItem(x_axis)

        # Y axis (green)
        y_axis = gl.GLLinePlotItem(
            pos=np.array([[0, 0, 0], [0, 0.5, 0]]),
            color=(0, 1, 0, 1),
            width=2.0
        )
        self.addItem(y_axis)

        # Z axis (blue)
        z_axis = gl.GLLinePlotItem(
            pos=np.array([[0, 0, 0], [0, 0, 0.5]]),
            color=(0, 0, 1, 1),
            width=2.0
        )
        self.addItem(z_axis)

    def update_trajectory(self, positions, colors):
        if len(positions) > 1:
            self.trajectory_item.setData(pos=positions, color=colors, width=3.0, antialias=True)
            # Update current position marker
            self.marker_item.setData(pos=positions[-1:], color=(1, 0, 0, 1), size=10)


class StatsPanel(QWidget):
    """Real-time statistics display panel"""
    def __init__(self):
        super().__init__()
        self.init_ui()

    def init_ui(self):
        layout = QHBoxLayout()
        layout.setContentsMargins(10, 5, 10, 5)

        # Status indicator
        self.status_label = QLabel("● Disconnected")
        self.status_label.setStyleSheet("color: #ff4757; font-weight: bold;")
        layout.addWidget(self.status_label)

        layout.addStretch()

        # Stats labels
        self.fps_label = QLabel("FPS: --")
        self.packets_label = QLabel("Packets: 0")
        self.drop_label = QLabel("Drop: 0")
        self.latency_label = QLabel("Latency: --")
        self.uptime_label = QLabel("Uptime: 00:00:00")

        for label in [self.fps_label, self.packets_label, self.drop_label,
                      self.latency_label, self.uptime_label]:
            label.setStyleSheet("color: #eaeaea;")
            layout.addWidget(label)
            layout.addSpacing(15)

        # Position labels
        self.pos_label = QLabel("X: -- Y: -- Z: --")
        self.pos_label.setStyleSheet("color: #00d9ff;")
        layout.addWidget(self.pos_label)

        layout.addSpacing(20)

        # Upload status
        self.upload_label = QLabel("Uploads: 0")
        self.upload_label.setStyleSheet("color: #a0a0a0;")
        layout.addWidget(self.upload_label)

        self.setLayout(layout)
        self.setStyleSheet("background-color: #16213e;")
        self.setFixedHeight(40)

        self.upload_count = 0

    def update_stats(self, stats):
        self.status_label.setText("● Connected")
        self.status_label.setStyleSheet("color: #00d9ff; font-weight: bold;")

        self.fps_label.setText(f"FPS: {stats['fps']:.1f}")
        self.packets_label.setText(f"Packets: {stats['packet_count']}")
        self.drop_label.setText(f"Drop: {stats['drop_count']}")
        self.latency_label.setText(f"Latency: {stats['latency_ms']:.1f}ms")

        # Format uptime
        uptime = int(stats['uptime'])
        hours = uptime // 3600
        minutes = (uptime % 3600) // 60
        seconds = uptime % 60
        self.uptime_label.setText(f"Uptime: {hours:02d}:{minutes:02d}:{seconds:02d}")

        x, y, z = stats['position']
        self.pos_label.setText(f"X: {x:+.3f}m  Y: {y:+.3f}m  Z: {z:+.3f}m")

    def set_disconnected(self):
        self.status_label.setText("● Disconnected")
        self.status_label.setStyleSheet("color: #ff4757; font-weight: bold;")

    def increment_upload_count(self, filename):
        self.upload_count += 1
        self.upload_label.setText(f"Uploads: {self.upload_count}")
        self.upload_label.setStyleSheet("color: #00d9ff;")
        # Flash effect
        QTimer.singleShot(500, lambda: self.upload_label.setStyleSheet("color: #a0a0a0;"))


class UploadAwareStatsPanel(QWidget):
    """Real-time statistics with upload progress and save-path visibility."""

    def __init__(self):
        super().__init__()
        self.upload_count = 0
        self.init_ui()

    def init_ui(self):
        root_layout = QVBoxLayout()
        root_layout.setContentsMargins(10, 6, 10, 8)
        root_layout.setSpacing(6)

        stats_row = QHBoxLayout()
        stats_row.setContentsMargins(0, 0, 0, 0)

        self.status_label = QLabel("Disconnected")
        self.status_label.setStyleSheet("color: #ff4757; font-weight: bold;")
        stats_row.addWidget(self.status_label)
        stats_row.addStretch()

        self.fps_label = QLabel("FPS: --")
        self.packets_label = QLabel("Packets: 0")
        self.drop_label = QLabel("Drop: 0")
        self.latency_label = QLabel("Latency: --")
        self.uptime_label = QLabel("Uptime: 00:00:00")
        for label in [self.fps_label, self.packets_label, self.drop_label, self.latency_label, self.uptime_label]:
            label.setStyleSheet("color: #eaeaea;")
            stats_row.addWidget(label)
            stats_row.addSpacing(15)

        self.pos_label = QLabel("X: -- Y: -- Z: --")
        self.pos_label.setStyleSheet("color: #00d9ff;")
        stats_row.addWidget(self.pos_label)
        stats_row.addSpacing(20)

        self.upload_label = QLabel("Uploads: 0")
        self.upload_label.setStyleSheet("color: #a0a0a0;")
        stats_row.addWidget(self.upload_label)
        root_layout.addLayout(stats_row)

        upload_row = QHBoxLayout()
        upload_row.setContentsMargins(0, 0, 0, 0)
        upload_row.setSpacing(10)

        self.upload_state_label = QLabel("Upload server idle")
        self.upload_state_label.setStyleSheet("color: #a0a0a0;")
        upload_row.addWidget(self.upload_state_label)

        self.upload_progress_label = QLabel("--")
        self.upload_progress_label.setStyleSheet("color: #8bd3ff;")
        upload_row.addWidget(self.upload_progress_label)

        self.upload_target_label = QLabel("")
        self.upload_target_label.setStyleSheet("color: #8f9bb3;")
        upload_row.addWidget(self.upload_target_label, 1)
        root_layout.addLayout(upload_row)

        self.upload_progress_bar = QProgressBar()
        self.upload_progress_bar.setRange(0, 100)
        self.upload_progress_bar.setValue(0)
        self.upload_progress_bar.setTextVisible(True)
        self.upload_progress_bar.setFormat("Waiting")
        self.upload_progress_bar.setFixedHeight(16)
        root_layout.addWidget(self.upload_progress_bar)

        self.upload_saved_label = QLabel(f"Upload folder: {get_default_upload_dir()}")
        self.upload_saved_label.setStyleSheet("color: #8f9bb3;")
        self.upload_saved_label.setWordWrap(True)
        root_layout.addWidget(self.upload_saved_label)

        self.setLayout(root_layout)
        self.setStyleSheet("background-color: #16213e;")
        self.setFixedHeight(94)

    def update_stats(self, stats):
        self.status_label.setText("Connected")
        self.status_label.setStyleSheet("color: #00d9ff; font-weight: bold;")
        self.fps_label.setText(f"FPS: {stats['fps']:.1f}")
        self.packets_label.setText(f"Packets: {stats['packet_count']}")
        self.drop_label.setText(f"Drop: {stats['drop_count']}")
        self.latency_label.setText(f"Latency: {stats['latency_ms']:.1f}ms")

        uptime = int(stats['uptime'])
        hours = uptime // 3600
        minutes = (uptime % 3600) // 60
        seconds = uptime % 60
        self.uptime_label.setText(f"Uptime: {hours:02d}:{minutes:02d}:{seconds:02d}")

        x, y, z = stats['position']
        self.pos_label.setText(f"X: {x:+.3f}m  Y: {y:+.3f}m  Z: {z:+.3f}m")

    def set_disconnected(self):
        self.status_label.setText("Disconnected")
        self.status_label.setStyleSheet("color: #ff4757; font-weight: bold;")

    def increment_upload_count(self, filename, saved_to=None):
        self.upload_count += 1
        self.upload_label.setText(f"Uploads: {self.upload_count}")
        self.upload_label.setStyleSheet("color: #00d9ff;")
        self.upload_state_label.setText(f"Received: {filename}")
        self.upload_state_label.setStyleSheet("color: #00d9ff; font-weight: bold;")
        self.upload_progress_bar.setValue(100)
        self.upload_progress_bar.setFormat("100%")
        self.upload_progress_label.setText("Complete")
        if saved_to:
            self.upload_saved_label.setText(f"Saved to: {saved_to}")
        QTimer.singleShot(500, lambda: self.upload_label.setStyleSheet("color: #a0a0a0;"))

    def set_upload_folder(self, upload_dir):
        self.upload_saved_label.setText(f"Upload folder: {Path(upload_dir).resolve()}")

    def set_upload_idle(self, upload_dir=None):
        self.upload_state_label.setText("Upload server idle")
        self.upload_state_label.setStyleSheet("color: #a0a0a0;")
        self.upload_progress_label.setText("--")
        self.upload_target_label.setText("")
        self.upload_progress_bar.setValue(0)
        self.upload_progress_bar.setFormat("Waiting")
        if upload_dir is not None:
            self.set_upload_folder(upload_dir)

    def set_upload_server_running(self, upload_dir):
        self.upload_state_label.setText("Upload server listening")
        self.upload_state_label.setStyleSheet("color: #00d9ff; font-weight: bold;")
        self.upload_progress_label.setText("Ready")
        self.upload_target_label.setText("")
        self.upload_progress_bar.setValue(0)
        self.upload_progress_bar.setFormat("Waiting")
        self.set_upload_folder(upload_dir)

    def update_upload_progress(self, event):
        stage = event.get("stage", "receiving")
        filename = event.get("filename", "unknown")
        upload_kind = event.get("upload_kind", "file")
        bytes_received = int(event.get("bytes_received", 0) or 0)
        total_bytes = int(event.get("total_bytes", 0) or 0)
        percent = int(event.get("percent", 0) or 0)
        target_dir = event.get("target_dir", "")
        saved_to = event.get("saved_to", "")
        message = event.get("message", "")

        self.upload_progress_bar.setValue(max(0, min(100, percent)))
        self.upload_progress_bar.setFormat(f"{percent}%")
        self.upload_target_label.setText(Path(target_dir).name if target_dir else "")

        if stage == "starting":
            self.upload_state_label.setText(f"Receiving {upload_kind}: {filename}")
            self.upload_state_label.setStyleSheet("color: #ffd166; font-weight: bold;")
            self.upload_progress_label.setText(f"0% of {format_bytes(total_bytes)}")
            if target_dir:
                self.upload_saved_label.setText(f"Target folder: {target_dir}")
            return

        if stage == "receiving":
            size_text = format_bytes(total_bytes) if total_bytes else "unknown"
            self.upload_state_label.setText(f"Receiving {upload_kind}: {filename}")
            self.upload_state_label.setStyleSheet("color: #ffd166; font-weight: bold;")
            self.upload_progress_label.setText(f"{percent}%  {format_bytes(bytes_received)} / {size_text}")
            if target_dir:
                self.upload_saved_label.setText(f"Target folder: {target_dir}")
            return

        if stage == "completed":
            self.upload_state_label.setText(f"Upload complete: {filename}")
            self.upload_state_label.setStyleSheet("color: #00d9ff; font-weight: bold;")
            self.upload_progress_label.setText(f"Saved {format_bytes(total_bytes)}")
            if saved_to:
                self.upload_saved_label.setText(f"Saved to: {saved_to}")
            return

        if stage == "error":
            self.upload_state_label.setText(f"Upload failed: {filename}")
            self.upload_state_label.setStyleSheet("color: #ff6b6b; font-weight: bold;")
            self.upload_progress_label.setText(message or "Error")
            if target_dir:
                self.upload_saved_label.setText(f"Target folder: {target_dir}")


class ControlPanel(QWidget):
    """Control panel widget"""
    start_clicked = pyqtSignal()
    stop_clicked = pyqtSignal()
    upload_server_toggled = pyqtSignal(bool)  # True = start, False = stop
    reset_origin_clicked = pyqtSignal()
    open_folder_clicked = pyqtSignal()
    mode_changed = pyqtSignal(bool)  # True = all history, False = last 5s

    def __init__(self, host="0.0.0.0", port=5555, upload_port=8000):
        super().__init__()
        self.host = host
        self.port = port
        self.upload_port = upload_port
        self.local_ip = get_local_ip()
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(15)

        # Local IP Info group
        ip_group = QGroupBox("Local IP (for iPhone)")
        ip_layout = QVBoxLayout()

        ip_container = QHBoxLayout()
        self.ip_label = QLabel(self.local_ip)
        self.ip_label.setStyleSheet("color: #00d9ff; font-size: 16px; font-weight: bold;")
        ip_container.addWidget(self.ip_label)

        self.copy_ip_btn = QPushButton("Copy")
        self.copy_ip_btn.setFixedWidth(60)
        self.copy_ip_btn.clicked.connect(self.copy_ip_to_clipboard)
        ip_container.addWidget(self.copy_ip_btn)

        ip_layout.addLayout(ip_container)

        # Port info
        port_info = QLabel(f"UDP Port: {self.port}\nUpload Port: {self.upload_port}")
        port_info.setStyleSheet("color: #a0a0a0; font-size: 11px;")
        ip_layout.addWidget(port_info)

        ip_group.setLayout(ip_layout)
        layout.addWidget(ip_group)

        # Services group
        services_group = QGroupBox("Services")
        services_layout = QVBoxLayout()

        # UDP Receiver checkbox
        self.udp_checkbox = QCheckBox("Enable UDP Receiver")
        self.udp_checkbox.setChecked(False)
        self.udp_checkbox.stateChanged.connect(self.on_udp_toggled)
        services_layout.addWidget(self.udp_checkbox)

        # Upload Server checkbox
        self.upload_checkbox = QCheckBox("Enable Upload Server")
        self.upload_checkbox.setChecked(False)
        self.upload_checkbox.stateChanged.connect(self.on_upload_toggled)
        services_layout.addWidget(self.upload_checkbox)

        services_group.setLayout(services_layout)
        layout.addWidget(services_group)

        # Display mode group
        display_group = QGroupBox("Display Mode")
        display_layout = QVBoxLayout()

        self.mode_group = QButtonGroup()
        self.radio_last5s = QRadioButton("Last 5 seconds")
        self.radio_all = QRadioButton("All history")
        self.radio_last5s.setChecked(True)

        self.mode_group.addButton(self.radio_last5s, 0)
        self.mode_group.addButton(self.radio_all, 1)

        self.radio_last5s.toggled.connect(lambda checked: self.mode_changed.emit(not checked))

        display_layout.addWidget(self.radio_last5s)
        display_layout.addWidget(self.radio_all)

        display_group.setLayout(display_layout)
        layout.addWidget(display_group)

        # Actions group
        actions_group = QGroupBox("Actions")
        actions_layout = QVBoxLayout()

        self.reset_btn = QPushButton("Reset Origin")
        self.reset_btn.clicked.connect(self.reset_origin_clicked.emit)
        actions_layout.addWidget(self.reset_btn)

        self.folder_btn = QPushButton("Open Data Folder")
        self.folder_btn.clicked.connect(self.open_folder_clicked.emit)
        actions_layout.addWidget(self.folder_btn)

        actions_group.setLayout(actions_layout)
        layout.addWidget(actions_group)

        layout.addStretch()

        self.setLayout(layout)
        self.setFixedWidth(280)

    def copy_ip_to_clipboard(self):
        clipboard = QApplication.clipboard()
        clipboard.setText(self.local_ip)
        # Visual feedback
        original_text = self.copy_ip_btn.text()
        self.copy_ip_btn.setText("Copied!")
        QTimer.singleShot(1000, lambda: self.copy_ip_btn.setText(original_text))

    def on_udp_toggled(self, state):
        if state == Qt.CheckState.Checked.value:
            self.start_clicked.emit()
        else:
            self.stop_clicked.emit()

    def on_upload_toggled(self, state):
        self.upload_server_toggled.emit(state == Qt.CheckState.Checked.value)

    def get_connection_params(self):
        return self.host, self.port

    def set_udp_running(self, running):
        self.udp_checkbox.setChecked(running)

    def set_upload_running(self, running):
        self.upload_checkbox.setChecked(running)


class MainWindow(QMainWindow):
    """Main application window"""
    def __init__(self, host="0.0.0.0", port=5555, upload_port=8000):
        super().__init__()
        self.host = host
        self.port = port
        self.upload_port = upload_port
        self.upload_dir = get_default_upload_dir()
        self.receiver_thread = None
        self.upload_server_thread = None
        self.trajectory_manager = TrajectoryManager()
        self.current_position = None

        self.init_ui()
        self.apply_stylesheet()

        # Update timer for 3D rendering (30 Hz)
        self.render_timer = QTimer()
        self.render_timer.timeout.connect(self.update_visualization)
        self.render_timer.start(33)  # ~30 FPS

    def init_ui(self):
        self.setWindowTitle("ARPose Visualizer")
        self.setGeometry(100, 100, 1280, 800)

        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # Main layout
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Top area (control panel + 3D view)
        top_layout = QHBoxLayout()
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)

        # Control panel
        self.control_panel = ControlPanel(self.host, self.port, self.upload_port)
        self.control_panel.start_clicked.connect(self.start_receiver)
        self.control_panel.stop_clicked.connect(self.stop_receiver)
        self.control_panel.upload_server_toggled.connect(self.toggle_upload_server)
        self.control_panel.reset_origin_clicked.connect(self.reset_origin)
        self.control_panel.open_folder_clicked.connect(self.open_data_folder)
        self.control_panel.mode_changed.connect(self.change_display_mode)
        top_layout.addWidget(self.control_panel)

        # 3D visualizer
        self.visualizer = Visualizer3D()
        top_layout.addWidget(self.visualizer, 1)

        main_layout.addLayout(top_layout, 1)

        # Stats panel
        self.stats_panel = UploadAwareStatsPanel()
        self.stats_panel.set_upload_idle(self.upload_dir)
        main_layout.addWidget(self.stats_panel)

        central_widget.setLayout(main_layout)

    def apply_stylesheet(self):
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1a1a2e;
            }
            QWidget {
                background-color: #16213e;
                color: #eaeaea;
                font-size: 13px;
            }
            QPushButton {
                background-color: #0f3460;
                color: #eaeaea;
                border: none;
                border-radius: 8px;
                padding: 10px 20px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #1a4d7a;
            }
            QPushButton:pressed {
                background-color: #e94560;
            }
            QPushButton:disabled {
                background-color: #2a2a3e;
                color: #666;
            }
            QGroupBox {
                border: 2px solid #0f3460;
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 10px;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QLineEdit {
                background-color: #1a1a2e;
                border: 2px solid #0f3460;
                border-radius: 5px;
                padding: 5px;
                color: #eaeaea;
            }
            QLineEdit:focus {
                border: 2px solid #00d9ff;
            }
            QRadioButton {
                spacing: 5px;
            }
            QRadioButton::indicator {
                width: 18px;
                height: 18px;
            }
            QRadioButton::indicator:unchecked {
                border: 2px solid #0f3460;
                border-radius: 9px;
                background-color: #1a1a2e;
            }
            QRadioButton::indicator:checked {
                border: 2px solid #00d9ff;
                border-radius: 9px;
                background-color: #00d9ff;
            }
            QCheckBox {
                spacing: 5px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border: 2px solid #0f3460;
                border-radius: 4px;
                background-color: #1a1a2e;
            }
            QCheckBox::indicator:checked {
                border: 2px solid #00d9ff;
                background-color: #00d9ff;
            }
            QProgressBar {
                border: 1px solid #0f3460;
                border-radius: 6px;
                background-color: #1a1a2e;
                color: #eaeaea;
                text-align: center;
            }
            QProgressBar::chunk {
                background-color: #00d9ff;
                border-radius: 5px;
            }
            QLabel {
                color: #eaeaea;
            }
        """)

    def start_receiver(self):
        if self.receiver_thread is not None:
            return

        host, port = self.control_panel.get_connection_params()

        self.receiver_thread = UDPReceiverThread(host, port, "binary")
        self.receiver_thread.packet_received.connect(self.on_packet_received)
        self.receiver_thread.stats_updated.connect(self.on_stats_updated)
        self.receiver_thread.error_occurred.connect(self.on_error)
        self.receiver_thread.start()

        self.control_panel.set_udp_running(True)

    def stop_receiver(self):
        if self.receiver_thread is not None:
            self.receiver_thread.stop()
            self.receiver_thread.wait()
            self.receiver_thread = None

        self.control_panel.set_udp_running(False)
        self.stats_panel.set_disconnected()

    def toggle_upload_server(self, enable):
        if enable:
            self.start_upload_server()
        else:
            self.stop_upload_server()

    def start_upload_server(self):
        if self.upload_server_thread is not None:
            return

        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.upload_server_thread = UploadServerThread(self.upload_port, self.upload_dir)
        self.upload_server_thread.server_started.connect(self.on_upload_server_started)
        self.upload_server_thread.file_received.connect(self.on_file_received)
        self.upload_server_thread.upload_progress.connect(self.on_upload_progress)
        self.upload_server_thread.error_occurred.connect(self.on_upload_server_error)
        self.upload_server_thread.start()

    def stop_upload_server(self):
        if self.upload_server_thread is not None:
            self.upload_server_thread.stop()
            self.upload_server_thread.wait()
            self.upload_server_thread = None

        self.control_panel.set_upload_running(False)
        self.stats_panel.set_upload_idle(self.upload_dir)

    def on_upload_server_started(self, url):
        print(f"[INFO] Upload server started: {url}")
        self.control_panel.set_upload_running(True)
        self.stats_panel.set_upload_server_running(self.upload_dir)

    def on_file_received(self, capture_id, filename, saved_to):
        print(f"[INFO] File received: {capture_id}/{filename} -> {saved_to}")
        self.stats_panel.increment_upload_count(filename, saved_to)

    def on_upload_progress(self, event):
        self.stats_panel.update_upload_progress(event)

    def on_upload_server_error(self, error_msg):
        print(f"[ERROR] Upload server error: {error_msg}")
        self.control_panel.set_upload_running(False)
        self.stats_panel.set_upload_idle(self.upload_dir)

    def on_packet_received(self, data):
        position = data['position']
        timestamp = data['timestamp']
        self.current_position = position
        self.trajectory_manager.add_point(position, timestamp)

    def on_stats_updated(self, stats):
        self.stats_panel.update_stats(stats)

    def on_error(self, error_msg):
        print(f"Error: {error_msg}")

    def update_visualization(self):
        positions, timestamps, colors = self.trajectory_manager.get_trajectory()
        if len(positions) > 1:
            self.visualizer.update_trajectory(positions, colors)

    def reset_origin(self):
        if self.current_position is not None:
            self.trajectory_manager.reset_origin(self.current_position)

    def change_display_mode(self, show_all):
        self.trajectory_manager.show_all = show_all

    def open_data_folder(self):
        folder = self.upload_dir
        folder.mkdir(parents=True, exist_ok=True)

        if platform.system() == 'Darwin':  # macOS
            subprocess.run(['open', str(folder)])
        elif platform.system() == 'Windows':
            os.startfile(str(folder))

    def closeEvent(self, event):
        self.stop_receiver()
        self.stop_upload_server()
        event.accept()


def check_dependencies():
    """Check if all required dependencies are available"""
    try:
        import OpenGL
        from PyQt6.QtWidgets import QApplication
        import pyqtgraph.opengl as gl
        return True
    except ImportError as e:
        print(f"[ERROR] Missing dependency: {e}")
        print("\nPlease install dependencies with:")
        print("  pip install -r requirements_visualizer.txt")
        print("\nOr run the dependency checker:")
        print("  python check_visualizer_deps.py")
        return False


def main():
    parser = argparse.ArgumentParser(description="ARPose 3D Visualizer")
    parser.add_argument("--host", default="0.0.0.0", help="Host/IP to bind to")
    parser.add_argument("--port", type=int, default=5555, help="UDP port to bind to")
    parser.add_argument("--upload-port", type=int, default=8000, help="HTTP upload server port")
    args = parser.parse_args()

    # Enable high DPI support
    if hasattr(Qt.ApplicationAttribute, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)
    if hasattr(Qt.ApplicationAttribute, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    window = MainWindow(args.host, args.port, args.upload_port)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    if not check_dependencies():
        sys.exit(1)
    main()
