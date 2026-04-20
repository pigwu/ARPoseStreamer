import argparse
import colorsys
import os
import platform
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
import pyqtgraph.opengl as gl
from PyQt6.QtCore import QThread, QTimer, pyqtSignal, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QRadioButton, QButtonGroup, QLineEdit, QGroupBox
)

# UDP packet format (from udp_pose_receiver.py)
FLOAT32_PACKET = struct.Struct("<Id7f")

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

        self.setLayout(layout)
        self.setStyleSheet("background-color: #16213e;")
        self.setFixedHeight(40)

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


class ControlPanel(QWidget):
    """Control panel widget"""
    start_clicked = pyqtSignal()
    stop_clicked = pyqtSignal()
    reset_origin_clicked = pyqtSignal()
    open_folder_clicked = pyqtSignal()
    mode_changed = pyqtSignal(bool)  # True = all history, False = last 5s

    def __init__(self, host="0.0.0.0", port=5555):
        super().__init__()
        self.host = host
        self.port = port
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(15)

        # Connection group
        conn_group = QGroupBox("Connection")
        conn_layout = QVBoxLayout()

        self.host_input = QLineEdit(self.host)
        self.host_input.setPlaceholderText("Host (e.g., 0.0.0.0)")
        conn_layout.addWidget(QLabel("Host:"))
        conn_layout.addWidget(self.host_input)

        self.port_input = QLineEdit(str(self.port))
        self.port_input.setPlaceholderText("Port (e.g., 5555)")
        conn_layout.addWidget(QLabel("Port:"))
        conn_layout.addWidget(self.port_input)

        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self.start_clicked.emit)
        conn_layout.addWidget(self.start_btn)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_clicked.emit)
        self.stop_btn.setEnabled(False)
        conn_layout.addWidget(self.stop_btn)

        conn_group.setLayout(conn_layout)
        layout.addWidget(conn_group)

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
        self.setFixedWidth(250)

    def get_connection_params(self):
        return self.host_input.text(), int(self.port_input.text())

    def set_running(self, running):
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.host_input.setEnabled(not running)
        self.port_input.setEnabled(not running)


class MainWindow(QMainWindow):
    """Main application window"""
    def __init__(self, host="0.0.0.0", port=5555):
        super().__init__()
        self.host = host
        self.port = port
        self.receiver_thread = None
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
        self.control_panel = ControlPanel(self.host, self.port)
        self.control_panel.start_clicked.connect(self.start_receiver)
        self.control_panel.stop_clicked.connect(self.stop_receiver)
        self.control_panel.reset_origin_clicked.connect(self.reset_origin)
        self.control_panel.open_folder_clicked.connect(self.open_data_folder)
        self.control_panel.mode_changed.connect(self.change_display_mode)
        top_layout.addWidget(self.control_panel)

        # 3D visualizer
        self.visualizer = Visualizer3D()
        top_layout.addWidget(self.visualizer, 1)

        main_layout.addLayout(top_layout, 1)

        # Stats panel
        self.stats_panel = StatsPanel()
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

        self.control_panel.set_running(True)

    def stop_receiver(self):
        if self.receiver_thread is not None:
            self.receiver_thread.stop()
            self.receiver_thread.wait()
            self.receiver_thread = None

        self.control_panel.set_running(False)
        self.stats_panel.set_disconnected()

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
        folder = Path.home() / 'Documents' / 'ARPoseStreamer'
        folder.mkdir(parents=True, exist_ok=True)

        if platform.system() == 'Darwin':  # macOS
            subprocess.run(['open', str(folder)])
        elif platform.system() == 'Windows':
            os.startfile(str(folder))

    def closeEvent(self, event):
        self.stop_receiver()
        event.accept()


def main():
    parser = argparse.ArgumentParser(description="ARPose 3D Visualizer")
    parser.add_argument("--host", default="0.0.0.0", help="Host/IP to bind to")
    parser.add_argument("--port", type=int, default=5555, help="UDP port to bind to")
    args = parser.parse_args()

    # Enable high DPI support
    if hasattr(Qt.ApplicationAttribute, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)
    if hasattr(Qt.ApplicationAttribute, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    window = MainWindow(args.host, args.port)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
