# -*- coding: utf-8 -*-
import argparse
import csv
import math
import os
import socket
import struct
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import numpy as np

try:
    import pyqtgraph as pg
except Exception:  # pragma: no cover - table/status UI still works.
    pg = None

from PyQt6.QtCore import QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


UDP_HOST = "0.0.0.0"
UDP_PORT = 5555
NUM_CHIPS = 5
MAGIC = 0x41534B4E  # "ASKN"
PACKET_FORMAT = "<IIQ" + "f" * (NUM_CHIPS * 4)
PACKET_STRUCT = struct.Struct(PACKET_FORMAT)
PACKET_SIZE = PACKET_STRUCT.size
ROLLING_FPS_SECONDS = 5.0
MAX_HISTORY_POINTS = 6000
SEQUENCE_RESET_DISTANCE = 10000


def decode_anyskin_packet(packet: bytes):
    if len(packet) != PACKET_SIZE:
        raise ValueError(f"Expected {PACKET_SIZE} bytes, got {len(packet)}")

    unpacked = PACKET_STRUCT.unpack(packet)
    magic = unpacked[0]
    if magic != MAGIC:
        raise ValueError(f"Wrong magic: {hex(magic)}")

    seq = int(unpacked[1])
    mcu_time_us = int(unpacked[2])
    values = np.asarray(unpacked[3:], dtype=np.float32).reshape(NUM_CHIPS, 4)
    return seq, mcu_time_us, values


def get_local_ip() -> str:
    ips = get_local_ips()
    return ips[0] if ips else "127.0.0.1"


def get_local_ips() -> List[str]:
    candidates = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        candidates.append(sock.getsockname()[0])
        sock.close()
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = item[4][0]
            if ip and not ip.startswith("127."):
                candidates.append(ip)
    except Exception:
        pass

    unique = []
    for ip in candidates:
        if ip not in unique:
            unique.append(ip)
    return unique or ["127.0.0.1"]


def format_float(value, digits=3):
    if value is None or not math.isfinite(float(value)):
        return "--"
    return f"{float(value):.{digits}f}"


def format_int(value: int) -> str:
    return f"{int(value):,}"


class AnySkinReceiverThread(QThread):
    packet_received = pyqtSignal(dict)
    status_changed = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(self, host: str, port: int):
        super().__init__()
        self.host = host
        self.port = int(port)
        self.running = False
        self.sock = None

    def run(self):
        self.running = True
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((self.host, self.port))
            self.sock.settimeout(0.2)
            self.status_changed.emit(f"Listening on {self.host}:{self.port}")

            while self.running:
                try:
                    packet, address = self.sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    if self.running:
                        self.error_occurred.emit("Socket closed unexpectedly")
                    break

                recv_wall_time = time.time()
                recv_monotonic_time = time.monotonic()
                try:
                    seq, mcu_time_us, values = decode_anyskin_packet(packet)
                except Exception as exc:
                    self.error_occurred.emit(f"Decode error from {address[0]}:{address[1]}: {exc}")
                    continue

                self.packet_received.emit(
                    {
                        "address": address,
                        "packet_size": len(packet),
                        "recv_wall_time": recv_wall_time,
                        "recv_monotonic_time": recv_monotonic_time,
                        "seq": seq,
                        "mcu_time_us": mcu_time_us,
                        "values": values,
                    }
                )
        except Exception as exc:
            self.error_occurred.emit(f"UDP listener failed: {exc}")
        finally:
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None
            self.running = False
            self.status_changed.emit("Stopped")

    def stop(self):
        self.running = False


class MetricCard(QFrame):
    def __init__(self, title: str, accent: str = "#2563eb"):
        super().__init__()
        self.accent = accent
        self.setObjectName("MetricCard")
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("MetricTitle")
        self.value_label = QLabel("--")
        self.value_label.setObjectName("MetricValue")
        font = QFont()
        font.setPointSize(20)
        font.setBold(True)
        self.value_label.setFont(font)
        self.detail_label = QLabel("")
        self.detail_label.setObjectName("MetricDetail")
        self.detail_label.setWordWrap(True)

        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.detail_label)

    def set_metric(self, value: str, detail: str = "", accent: Optional[str] = None):
        self.value_label.setText(value)
        self.detail_label.setText(detail)
        self.value_label.setStyleSheet(f"color: {accent or self.accent};")


class AnySkinMonitorWindow(QMainWindow):
    def __init__(self, host: str, port: int, auto_start: bool = False):
        super().__init__()
        self.setWindowTitle("AnySkin UDP Monitor")
        self.resize(1420, 900)
        self.setMinimumSize(1120, 760)
        self.local_ips = get_local_ips()
        self.receiver_thread = None
        self.csv_file = None
        self.csv_writer = None
        self.csv_path = None
        self.last_csv_flush = 0.0

        self.reset_stats(quiet=True)
        self.build_ui(host, port)
        self.apply_styles()

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self.refresh_stats)
        self.ui_timer.start(250)

        self.plot_timer = QTimer(self)
        self.plot_timer.timeout.connect(self.refresh_plots)
        self.plot_timer.start(100)

        if auto_start:
            QTimer.singleShot(150, self.start_receiver)

    def build_ui(self, host: str, port: int):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)
        self.setCentralWidget(central)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("AnySkin UDP Monitor")
        title.setObjectName("WindowTitle")
        subtitle = QLabel(f"PC IP candidates for board: {', '.join(self.local_ips)} | UDP port {port}")
        subtitle.setObjectName("Subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        root.addLayout(header)

        controls = QGridLayout()
        controls.setHorizontalSpacing(10)
        controls.setVerticalSpacing(8)

        self.host_edit = QLineEdit(host)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(port)
        self.history_spin = QSpinBox()
        self.history_spin.setRange(5, 300)
        self.history_spin.setValue(30)
        self.log_dir_edit = QLineEdit(str(Path.cwd() / "logs"))
        self.save_csv_check = QCheckBox("Save CSV")
        self.save_csv_check.setChecked(True)

        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.reset_button = QPushButton("Reset Stats")
        self.browse_log_button = QPushButton("Browse")
        self.open_log_button = QPushButton("Open Log Folder")

        controls.addWidget(QLabel("Bind host"), 0, 0)
        controls.addWidget(self.host_edit, 0, 1)
        controls.addWidget(QLabel("UDP port"), 0, 2)
        controls.addWidget(self.port_spin, 0, 3)
        controls.addWidget(QLabel("Plot window (s)"), 0, 4)
        controls.addWidget(self.history_spin, 0, 5)
        controls.addWidget(self.start_button, 0, 6)
        controls.addWidget(self.stop_button, 0, 7)

        controls.addWidget(QLabel("CSV folder"), 1, 0)
        controls.addWidget(self.log_dir_edit, 1, 1, 1, 4)
        controls.addWidget(self.save_csv_check, 1, 5)
        controls.addWidget(self.browse_log_button, 1, 6)
        controls.addWidget(self.open_log_button, 1, 7)
        controls.addWidget(self.reset_button, 1, 8)
        root.addLayout(controls)

        self.start_button.clicked.connect(self.start_receiver)
        self.stop_button.clicked.connect(self.stop_receiver)
        self.reset_button.clicked.connect(self.reset_stats)
        self.browse_log_button.clicked.connect(self.browse_log_folder)
        self.open_log_button.clicked.connect(lambda: open_folder(Path(self.log_dir_edit.text())))

        cards = QHBoxLayout()
        self.status_card = MetricCard("Status", "#2563eb")
        self.source_card = MetricCard("Source", "#0891b2")
        self.rate_card = MetricCard("Receive Rate", "#16a34a")
        self.loss_card = MetricCard("Loss", "#dc2626")
        self.seq_card = MetricCard("Sequence", "#7c3aed")
        for card in (self.status_card, self.source_card, self.rate_card, self.loss_card, self.seq_card):
            cards.addWidget(card)
        root.addLayout(cards)

        middle = QHBoxLayout()
        middle.setSpacing(16)
        left_panel = QVBoxLayout()
        left_panel.addWidget(section_label("Latest 5-chip values"))
        self.table = QTableWidget(NUM_CHIPS, 5)
        self.table.setHorizontalHeaderLabels(["Chip", "t", "x", "y", "z"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setMinimumWidth(560)
        self.table.setMinimumHeight(250)
        self.table.verticalHeader().setDefaultSectionSize(38)
        horizontal_header = self.table.horizontalHeader()
        horizontal_header.setStretchLastSection(False)
        horizontal_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        horizontal_header.resizeSection(0, 58)
        for col in range(1, 5):
            horizontal_header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        for row in range(NUM_CHIPS):
            chip_item = QTableWidgetItem(f"S{row}")
            chip_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 0, chip_item)
            for col in range(1, 5):
                item = QTableWidgetItem("--")
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, col, item)
        left_panel.addWidget(self.table)
        left_panel.addWidget(section_label("Events"))
        self.event_log = QPlainTextEdit()
        self.event_log.setReadOnly(True)
        self.event_log.setMaximumHeight(170)
        self.event_log.document().setMaximumBlockCount(300)
        left_panel.addWidget(self.event_log)
        middle.addLayout(left_panel, 1)

        right_panel = QVBoxLayout()
        plot_header = QHBoxLayout()
        plot_header.addWidget(section_label("Realtime plots"))
        plot_header.addStretch()
        plot_header.addWidget(QLabel("Selected chip"))
        self.sensor_combo = QComboBox()
        for index in range(NUM_CHIPS):
            self.sensor_combo.addItem(f"S{index}", index)
        plot_header.addWidget(self.sensor_combo)
        right_panel.addLayout(plot_header)

        if pg is None:
            self.axes_plot = QLabel("pyqtgraph is not installed. Install requirements_visualizer.txt to show plots.")
            self.axes_plot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.magnitude_plot = QLabel("")
            self.axis_curves = []
            self.mag_curves = []
        else:
            pg.setConfigOptions(antialias=True)
            self.axes_plot = pg.PlotWidget()
            self.axes_plot.setBackground("w")
            self.axes_plot.addLegend()
            self.axes_plot.setLabel("left", "value")
            self.axes_plot.setLabel("bottom", "window time", units="s")
            self.axes_plot.showGrid(x=True, y=True, alpha=0.25)
            self.axes_plot.setMouseEnabled(x=False, y=True)
            self.axes_plot.setXRange(0, self.history_spin.value(), padding=0)
            axis_colors = {"t": "#64748b", "x": "#ef4444", "y": "#22c55e", "z": "#2563eb"}
            self.axis_curves = [
                self.axes_plot.plot([], [], pen=pg.mkPen(color, width=2), name=name)
                for name, color in axis_colors.items()
            ]

            self.magnitude_plot = pg.PlotWidget()
            self.magnitude_plot.setBackground("w")
            self.magnitude_plot.addLegend()
            self.magnitude_plot.setLabel("left", "|xyz|")
            self.magnitude_plot.setLabel("bottom", "window time", units="s")
            self.magnitude_plot.showGrid(x=True, y=True, alpha=0.25)
            self.magnitude_plot.setMouseEnabled(x=False, y=True)
            self.magnitude_plot.setXRange(0, self.history_spin.value(), padding=0)
            self.magnitude_plot.setYRange(0, 1, padding=0)
            colors = ["#0f172a", "#dc2626", "#16a34a", "#2563eb", "#f97316"]
            self.mag_curves = [
                self.magnitude_plot.plot([], [], pen=pg.mkPen(colors[i], width=2), name=f"S{i}")
                for i in range(NUM_CHIPS)
            ]

        right_panel.addWidget(self.axes_plot, 1)
        right_panel.addWidget(self.magnitude_plot, 1)
        middle.addLayout(right_panel, 2)
        root.addLayout(middle, 1)

    def start_receiver(self):
        if self.receiver_thread is not None:
            return

        host = self.host_edit.text().strip() or UDP_HOST
        port = int(self.port_spin.value())
        self.csv_path = None
        self.reset_stats()

        if self.save_csv_check.isChecked():
            try:
                self.open_csv_logger()
            except Exception as exc:
                QMessageBox.critical(self, "CSV error", str(exc))
                return

        self.receiver_thread = AnySkinReceiverThread(host, port)
        self.receiver_thread.packet_received.connect(self.on_packet_received)
        self.receiver_thread.status_changed.connect(self.on_status_changed)
        self.receiver_thread.error_occurred.connect(self.on_error)
        self.receiver_thread.start()
        self.set_running(True)
        self.log_event(f"Expected packet size: {PACKET_SIZE} bytes")
        if self.csv_path:
            self.log_event(f"Saving CSV: {self.csv_path}")

    def stop_receiver(self):
        if self.receiver_thread is not None:
            self.receiver_thread.stop()
            self.receiver_thread.wait(1500)
            self.receiver_thread = None
        self.close_csv_logger()
        self.set_running(False)

    def reset_stats(self, quiet: bool = False):
        self.received = 0
        self.dropped = 0
        self.out_of_order = 0
        self.decode_errors = 0
        self.last_seq = None
        self.latest_packet = None
        self.latest_values = np.zeros((NUM_CHIPS, 4), dtype=np.float32)
        self.history = deque(maxlen=MAX_HISTORY_POINTS)
        self.packet_times = deque()
        self.start_monotonic = time.monotonic()
        if not quiet:
            self.log_event("Stats reset")

    def open_csv_logger(self):
        log_dir = Path(self.log_dir_edit.text()).expanduser().resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = log_dir / f"anyskin_udp_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        self.csv_file = self.csv_path.open("w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        header = ["pc_receive_time", "seq", "mcu_time_us"]
        for index in range(NUM_CHIPS):
            header.extend([f"s{index}_t", f"s{index}_x", f"s{index}_y", f"s{index}_z"])
        self.csv_writer.writerow(header)
        self.last_csv_flush = time.monotonic()

    def close_csv_logger(self):
        if self.csv_file is not None:
            try:
                self.csv_file.flush()
                self.csv_file.close()
            finally:
                self.csv_file = None
                self.csv_writer = None

    def on_packet_received(self, packet: dict):
        now = packet["recv_monotonic_time"]
        seq = packet["seq"]
        values = np.asarray(packet["values"], dtype=np.float32)

        if self.last_seq is None:
            self.last_seq = seq
        elif seq > self.last_seq:
            gap = seq - self.last_seq
            if gap > 1:
                self.dropped += gap - 1
            self.last_seq = seq
        elif seq == self.last_seq:
            self.out_of_order += 1
        elif self.last_seq - seq < SEQUENCE_RESET_DISTANCE:
            self.out_of_order += 1
        else:
            self.log_event(f"Sequence restart detected: {self.last_seq} -> {seq}")
            self.last_seq = seq

        self.received += 1
        self.latest_packet = packet
        self.latest_values = values
        elapsed = now - self.start_monotonic
        self.history.append((elapsed, values.copy()))
        self.packet_times.append(now)
        while self.packet_times and now - self.packet_times[0] > ROLLING_FPS_SECONDS:
            self.packet_times.popleft()

        self.update_table(values)
        self.write_csv_row(packet, values)

    def write_csv_row(self, packet: dict, values: np.ndarray):
        if self.csv_writer is None:
            return
        row = [packet["recv_wall_time"], packet["seq"], packet["mcu_time_us"]]
        row.extend(values.reshape(-1).tolist())
        self.csv_writer.writerow(row)
        now = time.monotonic()
        if now - self.last_csv_flush >= 1.0:
            self.csv_file.flush()
            self.last_csv_flush = now

    def update_table(self, values: np.ndarray):
        for row in range(NUM_CHIPS):
            for col in range(4):
                item = self.table.item(row, col + 1)
                item.setText(format_float(values[row, col], 2))

    def refresh_stats(self):
        if self.receiver_thread is None:
            self.status_card.set_metric("Idle", "Not listening", "#64748b")
        elif self.latest_packet is None:
            self.status_card.set_metric("Listening", f"{self.host_edit.text()}:{self.port_spin.value()}", "#2563eb")
        else:
            self.status_card.set_metric("Receiving", f"{PACKET_SIZE} bytes/packet", "#16a34a")

        if self.latest_packet:
            addr = self.latest_packet["address"]
            self.source_card.set_metric(addr[0], f"port {addr[1]}", "#0891b2")
            self.seq_card.set_metric(str(self.latest_packet["seq"]), f"mcu_time_us={self.latest_packet['mcu_time_us']}", "#7c3aed")
        else:
            self.source_card.set_metric("--", "Waiting for board", "#64748b")
            self.seq_card.set_metric("--", "No packet yet", "#64748b")

        rolling_fps = self.rolling_fps()
        elapsed = max(time.monotonic() - self.start_monotonic, 1e-6)
        overall_fps = self.received / elapsed
        self.rate_card.set_metric(
            f"{rolling_fps:.1f} Hz",
            f"received={format_int(self.received)} overall={overall_fps:.1f} Hz",
            "#16a34a",
        )

        denominator = self.received + self.dropped
        loss_rate = (self.dropped / denominator * 100.0) if denominator else 0.0
        if loss_rate > 1.0:
            quality = "check Wi-Fi"
            accent = "#dc2626"
        elif loss_rate > 0.2:
            quality = "watch"
            accent = "#ca8a04"
        else:
            quality = "good"
            accent = "#16a34a"
        detail = f"{quality} | dropped={format_int(self.dropped)} out_of_order={format_int(self.out_of_order)}"
        self.loss_card.set_metric(f"{loss_rate:.3f}%", detail, accent)

    def rolling_fps(self) -> float:
        if len(self.packet_times) < 2:
            return 0.0
        duration = max(self.packet_times[-1] - self.packet_times[0], 1e-6)
        return (len(self.packet_times) - 1) / duration

    def refresh_plots(self):
        if pg is None or not self.history:
            return

        history_seconds = float(self.history_spin.value())
        latest_time = self.history[-1][0]
        rows = [(t, values) for t, values in self.history if t >= latest_time - history_seconds]
        if not rows:
            return

        times = np.asarray([row[0] for row in rows], dtype=float)
        values = np.stack([row[1] for row in rows], axis=0)
        times = times - times[0]

        sensor_index = int(self.sensor_combo.currentData())
        for axis in range(4):
            self.axis_curves[axis].setData(times, values[:, sensor_index, axis])

        magnitude = np.linalg.norm(values[:, :, 1:4], axis=2)
        for chip_index in range(NUM_CHIPS):
            self.mag_curves[chip_index].setData(times, magnitude[:, chip_index])

        self.axes_plot.setXRange(0, history_seconds, padding=0)
        self.magnitude_plot.setXRange(0, history_seconds, padding=0)
        max_magnitude = float(np.nanmax(magnitude)) if magnitude.size else 1.0
        if math.isfinite(max_magnitude):
            self.magnitude_plot.setYRange(0, max(1.0, max_magnitude * 1.12), padding=0)

    def on_status_changed(self, message: str):
        self.log_event(message)
        if message == "Stopped":
            self.receiver_thread = None
            self.close_csv_logger()
            self.set_running(False)

    def on_error(self, message: str):
        if message.startswith("Decode error"):
            self.decode_errors += 1
        self.log_event(message)

    def browse_log_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select CSV log folder", self.log_dir_edit.text())
        if folder:
            self.log_dir_edit.setText(folder)

    def set_running(self, running: bool):
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.host_edit.setEnabled(not running)
        self.port_spin.setEnabled(not running)
        self.save_csv_check.setEnabled(not running)
        self.log_dir_edit.setEnabled(not running)
        self.browse_log_button.setEnabled(not running)

    def log_event(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.event_log.appendPlainText(f"[{timestamp}] {message}")

    def closeEvent(self, event):
        self.stop_receiver()
        event.accept()

    def apply_styles(self):
        self.setStyleSheet(
            """
            QWidget {
                background: #f8fafc;
                color: #0f172a;
                font-size: 13px;
            }
            QLabel#WindowTitle {
                font-size: 26px;
                font-weight: 700;
            }
            QLabel#Subtitle {
                color: #475569;
                font-size: 13px;
            }
            QLabel#SectionLabel {
                color: #1e293b;
                font-weight: 700;
            }
            QFrame#MetricCard {
                background: white;
                border: 1px solid #dbe3ef;
                border-radius: 8px;
            }
            QLabel#MetricTitle {
                color: #64748b;
                font-weight: 600;
            }
            QLabel#MetricDetail {
                color: #64748b;
            }
            QPushButton {
                background: #2563eb;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 12px;
                font-weight: 600;
            }
            QPushButton:disabled {
                background: #94a3b8;
            }
            QLineEdit, QSpinBox, QComboBox, QTableWidget, QPlainTextEdit {
                background: white;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 4px;
            }
            QHeaderView::section {
                background: #e2e8f0;
                color: #0f172a;
                padding: 6px;
                border: 1px solid #cbd5e1;
                font-weight: 700;
            }
            """
        )


def section_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SectionLabel")
    return label


def open_folder(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def main():
    parser = argparse.ArgumentParser(description="Realtime AnySkin UDP monitor with CSV logging.")
    parser.add_argument("--host", default=UDP_HOST, help="UDP host/IP to bind to.")
    parser.add_argument("--port", type=int, default=UDP_PORT, help="UDP port to listen on.")
    parser.add_argument("--auto-start", action="store_true", help="Start listening when the UI opens.")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    window = AnySkinMonitorWindow(args.host, args.port, auto_start=args.auto_start)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
