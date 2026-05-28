# -*- coding: utf-8 -*-
import argparse
import math
import socket
import struct
import sys
import time
from collections import deque
from statistics import pstdev
from typing import Optional

try:
    import pyqtgraph as pg
except Exception:  # pragma: no cover - the UI has a text fallback.
    pg = None

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


FLOAT32_PACKET = struct.Struct("<Id7f")
ROLLING_FPS_SECONDS = 5.0
MAX_HISTORY_POINTS = 300
SEQUENCE_RESET_DISTANCE = 10000


def decode_packet(packet: bytes, encoding: str):
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


def get_local_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


def format_int(value: int) -> str:
    return f"{value:,}"


def format_float(value, digits=1, suffix="") -> str:
    if value is None or not math.isfinite(float(value)):
        return "--"
    return f"{float(value):.{digits}f}{suffix}"


class UDPMonitorThread(QThread):
    packet_received = pyqtSignal(dict)
    status_changed = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(self, host: str, port: int, encoding: str):
        super().__init__()
        self.host = host
        self.port = port
        self.encoding = encoding
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
                    packet, address = self.sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    if self.running:
                        self.error_occurred.emit("Socket closed unexpectedly")
                    break

                recv_wall_time = time.time()
                recv_monotonic_time = time.monotonic()

                try:
                    sequence, sender_time, x, y, z, qx, qy, qz, qw = decode_packet(packet, self.encoding)
                except Exception as exc:
                    self.error_occurred.emit(f"Decode error from {address[0]}:{address[1]}: {exc}")
                    continue

                self.packet_received.emit(
                    {
                        "address": address,
                        "packet_size": len(packet),
                        "recv_wall_time": recv_wall_time,
                        "recv_monotonic_time": recv_monotonic_time,
                        "sequence": int(sequence),
                        "sender_time": float(sender_time),
                        "position": (float(x), float(y), float(z)),
                        "orientation": (float(qx), float(qy), float(qz), float(qw)),
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
        self.setMinimumHeight(108)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(5)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("MetricTitle")

        self.value_label = QLabel("--")
        self.value_label.setObjectName("MetricValue")
        value_font = QFont()
        value_font.setPointSize(24)
        value_font.setBold(True)
        self.value_label.setFont(value_font)

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


class PacketLossMonitorWindow(QMainWindow):
    def __init__(self, host: str, port: int, encoding: str):
        super().__init__()
        self.setWindowTitle("ARPoseStreamer UDP Packet Loss Monitor")
        self.resize(1220, 820)
        self.receiver_thread = None
        self.local_ip = get_local_ip()

        self._build_ui(host, port, encoding)
        self._apply_styles()
        self.reset_stats(quiet=True)

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self.refresh_ui)
        self.ui_timer.start(250)

        self.chart_timer = QTimer(self)
        self.chart_timer.timeout.connect(self.record_chart_sample)
        self.chart_timer.start(1000)

    def _build_ui(self, host: str, port: int, encoding: str):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(14)
        self.setCentralWidget(central)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("UDP 实时丢包率监测")
        title.setObjectName("WindowTitle")
        subtitle = QLabel(f"手机发送目标 / Phone target: {self.local_ip}:{port}")
        subtitle.setObjectName("Subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)

        self.status_pill = QLabel("Stopped")
        self.status_pill.setObjectName("StatusPill")
        self.status_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.status_pill)
        root.addLayout(header)

        controls = QGroupBox("监听设置")
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(14, 12, 14, 12)
        controls_layout.setSpacing(10)

        self.host_input = QLineEdit(host)
        self.host_input.setMinimumWidth(145)
        self.host_input.setPlaceholderText("0.0.0.0")
        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(port)
        self.port_input.setMinimumWidth(95)
        self.encoding_combo = QComboBox()
        self.encoding_combo.addItems(["binary", "csv"])
        self.encoding_combo.setCurrentText(encoding)
        self.encoding_combo.setMinimumWidth(105)

        self.start_button = QPushButton("开始监听")
        self.start_button.setObjectName("PrimaryButton")
        self.start_button.clicked.connect(self.toggle_receiver)
        self.reset_button = QPushButton("重置统计")
        self.reset_button.clicked.connect(self.reset_stats)

        controls_layout.addWidget(QLabel("Bind IP"))
        controls_layout.addWidget(self.host_input)
        controls_layout.addWidget(QLabel("Port"))
        controls_layout.addWidget(self.port_input)
        controls_layout.addWidget(QLabel("Encoding"))
        controls_layout.addWidget(self.encoding_combo)
        controls_layout.addStretch(1)
        controls_layout.addWidget(self.reset_button)
        controls_layout.addWidget(self.start_button)
        root.addWidget(controls)

        metrics = QGridLayout()
        metrics.setSpacing(12)
        self.loss_card = MetricCard("丢包率 Loss Rate", "#16a34a")
        self.dropped_card = MetricCard("丢包数 Dropped", "#dc2626")
        self.received_card = MetricCard("已接收 Received", "#2563eb")
        self.fps_card = MetricCard("实时速率 FPS", "#7c3aed")
        self.latency_card = MetricCard("近似延迟 Latency", "#0f766e")
        self.gap_card = MetricCard("最近跳号 Last Gap", "#ea580c")

        cards = [
            self.loss_card,
            self.dropped_card,
            self.received_card,
            self.fps_card,
            self.latency_card,
            self.gap_card,
        ]
        for index, card in enumerate(cards):
            metrics.addWidget(card, index // 3, index % 3)
        root.addLayout(metrics)

        lower = QHBoxLayout()
        lower.setSpacing(12)
        lower.addWidget(self._build_status_panel(), 0)
        lower.addWidget(self._build_chart_panel(), 1)
        root.addLayout(lower, 1)

        self.event_log = QPlainTextEdit()
        self.event_log.setObjectName("EventLog")
        self.event_log.setReadOnly(True)
        self.event_log.setMaximumHeight(120)
        root.addWidget(self.event_log)

    def _build_status_panel(self):
        panel = QGroupBox("当前数据")
        panel.setMinimumWidth(330)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(9)

        self.source_label = QLabel("Source: --")
        self.seq_label = QLabel("Latest sequence: --")
        self.expected_label = QLabel("Expected packets: --")
        self.unique_label = QLabel("Unique packets: --")
        self.ooo_label = QLabel("Out-of-order / duplicate: --")
        self.jitter_label = QLabel("Jitter: --")
        self.last_seen_label = QLabel("Last packet: --")
        self.position_label = QLabel("Position: --")
        self.position_label.setWordWrap(True)

        for label in [
            self.source_label,
            self.seq_label,
            self.expected_label,
            self.unique_label,
            self.ooo_label,
            self.jitter_label,
            self.last_seen_label,
            self.position_label,
        ]:
            label.setObjectName("StatusLine")
            layout.addWidget(label)
        layout.addStretch(1)
        return panel

    def _build_chart_panel(self):
        panel = QGroupBox("趋势图")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        if pg is None:
            fallback = QLabel("pyqtgraph is not installed. The numeric monitor still works.")
            fallback.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(fallback, 1)
            self.loss_plot = None
            self.fps_plot = None
            self.loss_curve = None
            self.fps_curve = None
            return panel

        pg.setConfigOptions(antialias=True)
        self.loss_plot = pg.PlotWidget()
        self.loss_plot.setBackground("#0f172a")
        self.loss_plot.setLabel("left", "Loss", units="%")
        self.loss_plot.setLabel("bottom", "Time", units="s")
        self.loss_plot.showGrid(x=True, y=True, alpha=0.22)
        self.loss_plot.setYRange(0, 10, padding=0)
        self.loss_curve = self.loss_plot.plot(pen=pg.mkPen("#ef4444", width=2))

        self.fps_plot = pg.PlotWidget()
        self.fps_plot.setBackground("#0f172a")
        self.fps_plot.setLabel("left", "FPS")
        self.fps_plot.setLabel("bottom", "Time", units="s")
        self.fps_plot.showGrid(x=True, y=True, alpha=0.22)
        self.fps_curve = self.fps_plot.plot(pen=pg.mkPen("#38bdf8", width=2))

        layout.addWidget(self.loss_plot, 1)
        layout.addWidget(self.fps_plot, 1)
        return panel

    def _apply_styles(self):
        self.setStyleSheet(
            """
            QMainWindow { background: #f6f8fb; }
            QLabel { color: #0f172a; font-size: 13px; }
            QGroupBox {
                background: #ffffff;
                border: 1px solid #dbe3ef;
                border-radius: 8px;
                margin-top: 12px;
                font-weight: 700;
                color: #1e293b;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }
            QLineEdit, QSpinBox, QComboBox {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 7px 8px;
                min-height: 22px;
                color: #0f172a;
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 8px 14px;
                color: #0f172a;
                font-weight: 700;
            }
            QPushButton:hover { background: #f1f5f9; }
            QPushButton:disabled { color: #94a3b8; background: #f8fafc; }
            QPushButton#PrimaryButton {
                background: #2563eb;
                border: 1px solid #1d4ed8;
                color: #ffffff;
                min-width: 116px;
            }
            QPushButton#PrimaryButton:hover { background: #1d4ed8; }
            QLabel#WindowTitle { font-size: 25px; font-weight: 800; color: #0f172a; }
            QLabel#Subtitle { color: #475569; font-size: 13px; }
            QLabel#StatusPill {
                background: #e2e8f0;
                color: #334155;
                border-radius: 14px;
                padding: 6px 14px;
                min-width: 128px;
                font-weight: 800;
            }
            QFrame#MetricCard {
                background: #ffffff;
                border: 1px solid #dbe3ef;
                border-radius: 8px;
            }
            QLabel#MetricTitle { color: #64748b; font-size: 12px; font-weight: 800; }
            QLabel#MetricValue { color: #2563eb; font-size: 28px; font-weight: 900; }
            QLabel#MetricDetail { color: #64748b; font-size: 12px; }
            QLabel#StatusLine { color: #334155; font-size: 13px; padding: 3px 0; }
            QPlainTextEdit#EventLog {
                background: #0f172a;
                border: 1px solid #1e293b;
                border-radius: 8px;
                color: #dbeafe;
                padding: 8px;
                font-family: Consolas, monospace;
                font-size: 12px;
            }
            """
        )

    def reset_stats(self, quiet=False):
        self.start_monotonic = time.monotonic()
        self.last_packet_monotonic = None
        self.last_packet_wall = None
        self.first_sequence = None
        self.max_sequence = None
        self.latest_sequence = None
        self.last_gap = 0
        self.packet_count = 0
        self.bytes_received = 0
        self.duplicate_count = 0
        self.out_of_order_count = 0
        self.decode_error_count = 0
        self.seen_sequences = set()
        self.arrival_times = deque()
        self.intervals = deque(maxlen=240)
        self.previous_arrival = None
        self.instant_fps = 0.0
        self.rolling_fps = 0.0
        self.last_latency_ms = None
        self.last_address = None
        self.last_position = None
        self.chart_times = deque(maxlen=MAX_HISTORY_POINTS)
        self.chart_loss = deque(maxlen=MAX_HISTORY_POINTS)
        self.chart_fps = deque(maxlen=MAX_HISTORY_POINTS)
        if not quiet:
            self.append_event("Counters reset")
        self.refresh_ui()

    def toggle_receiver(self):
        if self.receiver_thread is not None and self.receiver_thread.isRunning():
            self.stop_receiver()
            return
        self.start_receiver()

    def start_receiver(self):
        host = self.host_input.text().strip() or "0.0.0.0"
        port = int(self.port_input.value())
        encoding = self.encoding_combo.currentText()
        self.reset_stats(quiet=True)
        self.receiver_thread = UDPMonitorThread(host, port, encoding)
        self.receiver_thread.packet_received.connect(self.on_packet_received)
        self.receiver_thread.status_changed.connect(self.on_status_changed)
        self.receiver_thread.error_occurred.connect(self.on_error)
        self.receiver_thread.finished.connect(self.on_receiver_finished)
        self.receiver_thread.start()
        self.host_input.setEnabled(False)
        self.port_input.setEnabled(False)
        self.encoding_combo.setEnabled(False)
        self.start_button.setText("停止监听")
        self.append_event(f"Listening on {host}:{port} ({encoding})")
        self.refresh_ui()

    def stop_receiver(self):
        if self.receiver_thread is not None:
            self.receiver_thread.stop()
            self.receiver_thread.wait(1200)
        self.on_receiver_finished()

    def on_receiver_finished(self):
        self.host_input.setEnabled(True)
        self.port_input.setEnabled(True)
        self.encoding_combo.setEnabled(True)
        self.start_button.setText("开始监听")
        self.refresh_ui()

    def on_status_changed(self, text: str):
        self.status_pill.setText(text)
        if text == "Stopped":
            self.on_receiver_finished()

    def on_error(self, message: str):
        self.decode_error_count += 1
        self.append_event(message)
        self.refresh_ui()

    def on_packet_received(self, data: dict):
        seq = data["sequence"]
        now = data["recv_monotonic_time"]

        if (
            self.first_sequence is not None
            and self.max_sequence is not None
            and seq < self.first_sequence
            and self.max_sequence - seq > SEQUENCE_RESET_DISTANCE
        ):
            self.append_event("Sequence reset detected; counters restarted")
            self.reset_stats(quiet=True)

        self.packet_count += 1
        self.bytes_received += data["packet_size"]
        self.last_packet_monotonic = now
        self.last_packet_wall = data["recv_wall_time"]
        self.latest_sequence = seq
        self.last_address = f"{data['address'][0]}:{data['address'][1]}"
        self.last_position = data["position"]

        if self.previous_arrival is not None:
            interval = max(now - self.previous_arrival, 1e-9)
            self.instant_fps = 1.0 / interval
            self.intervals.append(interval)
        self.previous_arrival = now

        self.arrival_times.append(now)
        while self.arrival_times and now - self.arrival_times[0] > ROLLING_FPS_SECONDS:
            self.arrival_times.popleft()
        if len(self.arrival_times) >= 2:
            span = max(self.arrival_times[-1] - self.arrival_times[0], 1e-9)
            self.rolling_fps = (len(self.arrival_times) - 1) / span

        if seq in self.seen_sequences:
            self.duplicate_count += 1
            self.last_gap = 0
        else:
            self.seen_sequences.add(seq)
            if self.first_sequence is None:
                self.first_sequence = seq
                self.max_sequence = seq
                self.last_gap = 0
            elif seq > self.max_sequence:
                self.last_gap = seq - self.max_sequence - 1
                self.max_sequence = seq
            else:
                self.out_of_order_count += 1
                self.last_gap = 0

        latency_ms = (data["recv_wall_time"] - data["sender_time"]) * 1000.0
        self.last_latency_ms = latency_ms if -5000.0 <= latency_ms <= 60000.0 else None
        self.refresh_ui()

    def get_expected_count(self) -> int:
        if self.first_sequence is None or self.max_sequence is None:
            return 0
        return max(0, self.max_sequence - self.first_sequence + 1)

    def get_missing_count(self) -> int:
        return max(0, self.get_expected_count() - len(self.seen_sequences))

    def get_loss_rate(self) -> float:
        expected = self.get_expected_count()
        if expected <= 0:
            return 0.0
        return self.get_missing_count() / expected * 100.0

    def get_jitter_ms(self):
        if len(self.intervals) < 3:
            return None
        return pstdev(self.intervals) * 1000.0

    def get_loss_accent(self) -> str:
        loss_rate = self.get_loss_rate()
        if loss_rate >= 5.0:
            return "#dc2626"
        if loss_rate >= 1.0:
            return "#d97706"
        return "#16a34a"

    def refresh_ui(self):
        expected = self.get_expected_count()
        missing = self.get_missing_count()
        loss_rate = self.get_loss_rate()
        unique_count = len(self.seen_sequences)
        jitter_ms = self.get_jitter_ms()
        accent = self.get_loss_accent()

        self.loss_card.set_metric(f"{loss_rate:.2f}%", f"missing {format_int(missing)} / expected {format_int(expected)}", accent)
        self.dropped_card.set_metric(format_int(missing), f"gap-based missing packets", "#dc2626" if missing else "#16a34a")
        self.received_card.set_metric(format_int(self.packet_count), f"unique {format_int(unique_count)} packets", "#2563eb")
        self.fps_card.set_metric(format_float(self.rolling_fps, 1), f"instant {format_float(self.instant_fps, 1)} fps", "#7c3aed")
        self.latency_card.set_metric(format_float(self.last_latency_ms, 1, " ms"), "needs phone and PC clocks to match", "#0f766e")
        self.gap_card.set_metric(format_int(self.last_gap), f"decode errors {format_int(self.decode_error_count)}", "#ea580c" if self.last_gap else "#16a34a")

        self.source_label.setText(f"Source: {self.last_address or '--'}")
        self.seq_label.setText(f"Latest sequence: {self.latest_sequence if self.latest_sequence is not None else '--'}")
        self.expected_label.setText(f"Expected packets: {format_int(expected)}")
        self.unique_label.setText(f"Unique packets: {format_int(unique_count)}")
        self.ooo_label.setText(f"Out-of-order / duplicate: {format_int(self.out_of_order_count)} / {format_int(self.duplicate_count)}")
        self.jitter_label.setText(f"Jitter: {format_float(jitter_ms, 2, ' ms')}")
        self.last_seen_label.setText(f"Last packet: {self.format_last_seen()}")
        self.position_label.setText(f"Position: {self.format_position()}")

        if self.receiver_thread is not None and self.receiver_thread.isRunning():
            if self.last_packet_monotonic is None:
                self.status_pill.setText("Listening")
                self.status_pill.setStyleSheet("background: #dbeafe; color: #1d4ed8;")
            elif time.monotonic() - self.last_packet_monotonic > 2.0:
                self.status_pill.setText("Waiting")
                self.status_pill.setStyleSheet("background: #fef3c7; color: #92400e;")
            else:
                self.status_pill.setText("Receiving")
                self.status_pill.setStyleSheet("background: #dcfce7; color: #166534;")
        else:
            self.status_pill.setText("Stopped")
            self.status_pill.setStyleSheet("background: #e2e8f0; color: #334155;")

    def format_last_seen(self) -> str:
        if self.last_packet_monotonic is None:
            return "--"
        age = max(0.0, time.monotonic() - self.last_packet_monotonic)
        if age < 1.0:
            return "just now"
        return f"{age:.1f}s ago"

    def format_position(self) -> str:
        if self.last_position is None:
            return "--"
        x, y, z = self.last_position
        return f"x={x:+.3f}, y={y:+.3f}, z={z:+.3f}"

    def record_chart_sample(self):
        if self.start_monotonic is None:
            return
        elapsed = time.monotonic() - self.start_monotonic
        self.chart_times.append(elapsed)
        self.chart_loss.append(self.get_loss_rate())
        self.chart_fps.append(self.rolling_fps)
        self.update_charts()

    def update_charts(self):
        if pg is None or self.loss_curve is None or self.fps_curve is None:
            return
        xs = list(self.chart_times)
        if not xs:
            return
        self.loss_curve.setData(xs, list(self.chart_loss))
        self.fps_curve.setData(xs, list(self.chart_fps))
        max_loss = max(10.0, max(self.chart_loss, default=0.0) * 1.2)
        self.loss_plot.setYRange(0, max_loss, padding=0)
        max_fps = max(30.0, max(self.chart_fps, default=0.0) * 1.2)
        self.fps_plot.setYRange(0, max_fps, padding=0)

    def append_event(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        self.event_log.appendPlainText(f"[{timestamp}] {message}")

    def closeEvent(self, event):
        self.stop_receiver()
        event.accept()


def parse_args():
    parser = argparse.ArgumentParser(description="Desktop UDP packet-loss monitor for ARPoseStreamer.")
    parser.add_argument("--host", default="0.0.0.0", help="IP address to bind to.")
    parser.add_argument("--port", type=int, default=5555, help="UDP port to listen on.")
    parser.add_argument("--encoding", choices=("binary", "csv"), default="binary", help="Expected UDP packet encoding.")
    parser.add_argument("--no-auto-start", action="store_true", help="Open the monitor without immediately binding the UDP port.")
    return parser.parse_args()


def main():
    args = parse_args()
    app = QApplication(sys.argv)
    window = PacketLossMonitorWindow(args.host, args.port, args.encoding)
    window.show()
    if not args.no_auto_start:
        QTimer.singleShot(150, window.start_receiver)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
