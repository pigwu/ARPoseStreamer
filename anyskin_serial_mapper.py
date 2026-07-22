# -*- coding: utf-8 -*-
"""Realtime COM-port visualizer for identifying five magnetic sensor channels.

The tool accepts either newline-delimited text (15 XYZ or 20 T/XYZ values) or
the 96-byte ASKN v1 binary packet already used by the UDP magnetic path.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import struct
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import pyqtgraph as pg
except Exception:  # pragma: no cover - the numeric UI still remains usable.
    pg = None

try:
    import serial
    from serial.tools import list_ports
except Exception:  # pragma: no cover - shown as an actionable UI error.
    serial = None
    list_ports = None

from PyQt6.QtCore import QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


NUM_CHIPS = 5
ASKN_MAGIC = 0x41534B4E
ASKN_WIRE_MAGIC = b"NKSA"
ASKN_PACKET = struct.Struct("<IIQ20f")
MAX_BUFFER_BYTES = 64 * 1024
MAX_HISTORY_POINTS = 8000
NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
NUMBER_PATTERN = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
CHIP_LINE_RE = re.compile(r"\bchip\s*[:#]?\s*(\d+)\b", re.IGNORECASE)
CHIP_VALUE_RE = re.compile(
    rf"(?:^|[\s,;])([txyz])\s*=\s*({NUMBER_PATTERN})",
    re.IGNORECASE,
)
SERIES_COLORS = ["#2563eb", "#ef4444", "#16a34a", "#f59e0b", "#8b5cf6"]


def normalize_sensor_values(value) -> np.ndarray:
    """Normalize five-sensor values to rows of [temperature, x, y, z]."""
    array = np.asarray(value, dtype=np.float32)
    if array.shape == (NUM_CHIPS, 4):
        result = array.copy()
    elif array.shape == (NUM_CHIPS, 3):
        result = np.full((NUM_CHIPS, 4), np.nan, dtype=np.float32)
        result[:, 1:4] = array
    elif array.size == NUM_CHIPS * 4:
        result = array.reshape(NUM_CHIPS, 4).copy()
    elif array.size == NUM_CHIPS * 3:
        result = np.full((NUM_CHIPS, 4), np.nan, dtype=np.float32)
        result[:, 1:4] = array.reshape(NUM_CHIPS, 3)
    else:
        raise ValueError(f"需要 15 个 XYZ 或 20 个 T/XYZ 数值，实际收到 {array.size} 个")
    if not np.isfinite(result[:, 1:4]).all():
        raise ValueError("XYZ 中包含 NaN 或无穷大")
    return result


def _json_values(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("JSON 顶层必须是数组或对象")
    for key in ("values", "sensors", "data", "magnetic"):
        if key in payload:
            return payload[key]
    rows = []
    for index in range(NUM_CHIPS):
        row = None
        for key in (f"s{index}", f"S{index}", str(index)):
            if key in payload:
                row = payload[key]
                break
        if row is None:
            break
        if isinstance(row, dict):
            if "t" in row:
                row = [row["t"], row["x"], row["y"], row["z"]]
            else:
                row = [row["x"], row["y"], row["z"]]
        rows.append(row)
    if len(rows) == NUM_CHIPS:
        return rows
    raise ValueError("JSON 中未找到 values/sensors/data 或 S0-S4")


def _numeric_tokens(text: str) -> list[float]:
    values = []
    for token in re.split(r"[,;|\s]+", text.strip()):
        token = token.strip().strip("[](){}")
        if not token:
            continue
        if ":" in token or "=" in token:
            token = re.split(r"[:=]", token)[-1]
        if NUMBER_RE.fullmatch(token):
            values.append(float(token))
    return values


def parse_serial_line(line: str | bytes) -> np.ndarray:
    """Parse one text sample and return a (5, 4) T/XYZ array."""
    if isinstance(line, bytes):
        text = line.decode("utf-8", errors="strict").strip()
    else:
        text = str(line).strip()
    if not text:
        raise ValueError("空行")

    if text[0] in "[{":
        return normalize_sensor_values(_json_values(json.loads(text)))

    numbers = _numeric_tokens(text)
    count = len(numbers)
    if count in (15, 20):
        selected = numbers
    elif count in (16, 17):
        selected = numbers[-15:]
    elif count >= 20:
        # Common board lines prefix sequence/time before the 20 channel values.
        # A field explicitly named checksum/crc is discarded when it is numeric.
        lowered = text.lower()
        if count >= 21 and ("checksum" in lowered or "crc" in lowered):
            selected = numbers[-21:-1]
        else:
            selected = numbers[-20:]
    else:
        raise ValueError(f"无法识别：提取到 {count} 个数值")
    return normalize_sensor_values(selected)


def parse_chip_line(line: str | bytes) -> tuple[int, np.ndarray]:
    """Parse firmware output such as ``chip 1 ... t=24 x=1 y=2 z=3``."""
    if isinstance(line, bytes):
        text = line.decode("utf-8", errors="strict").strip()
    else:
        text = str(line).strip()
    chip_match = CHIP_LINE_RE.search(text)
    if chip_match is None:
        raise ValueError("not a per-chip line")
    fields = {name.lower(): float(value) for name, value in CHIP_VALUE_RE.findall(text)}
    missing = [name for name in ("t", "x", "y", "z") if name not in fields]
    if missing:
        raise ValueError(f"chip 行缺少字段：{', '.join(missing)}")
    row = np.asarray([fields["t"], fields["x"], fields["y"], fields["z"]], dtype=np.float32)
    if not np.isfinite(row).all():
        raise ValueError("chip 行包含 NaN 或无穷大")
    return int(chip_match.group(1)), row


def decode_askn_packet(packet: bytes) -> dict:
    if len(packet) != ASKN_PACKET.size:
        raise ValueError(f"ASKN 包应为 {ASKN_PACKET.size} 字节，实际 {len(packet)}")
    unpacked = ASKN_PACKET.unpack(packet)
    if unpacked[0] != ASKN_MAGIC:
        raise ValueError(f"ASKN magic 错误: 0x{unpacked[0]:08x}")
    values = normalize_sensor_values(unpacked[3:])
    return {
        "values": values,
        "sequence": int(unpacked[1]),
        "sensor_time": int(unpacked[2]),
        "source": "ASKN binary",
        "raw": f"ASKN seq={unpacked[1]} mcu_us={unpacked[2]}",
    }


class SerialStreamDecoder:
    """Incrementally decode text lines, binary ASKN frames, or both."""

    def __init__(self, mode: str = "auto"):
        if mode not in {"auto", "text", "binary"}:
            raise ValueError(f"unknown decoder mode: {mode}")
        self.mode = mode
        self.buffer = bytearray()
        self.chip_rows: dict[int, np.ndarray] = {}

    def feed(self, chunk: bytes) -> list[tuple[str, object]]:
        self.buffer.extend(chunk)
        output: list[tuple[str, object]] = []
        while self.buffer:
            if self.mode in {"auto", "binary"}:
                magic_at = self.buffer.find(ASKN_WIRE_MAGIC)
                newline_at = self.buffer.find(b"\n")
                # The binary float payload may itself contain byte 0x0a, so a
                # leading ASKN magic always takes precedence over text lines.
                should_decode_binary = magic_at == 0
                if should_decode_binary:
                    if len(self.buffer) < ASKN_PACKET.size:
                        break
                    packet = bytes(self.buffer[: ASKN_PACKET.size])
                    del self.buffer[: ASKN_PACKET.size]
                    try:
                        output.append(("frame", decode_askn_packet(packet)))
                    except Exception as exc:
                        output.append(("error", str(exc)))
                    continue
                if magic_at > 0 and (self.mode == "binary" or newline_at < 0 or magic_at < newline_at):
                    discarded = bytes(self.buffer[:magic_at])
                    del self.buffer[:magic_at]
                    if discarded.strip(b"\x00\r\n "):
                        output.append(("error", f"二进制包前丢弃 {len(discarded)} 字节"))
                    continue

            newline_at = self.buffer.find(b"\n")
            if self.mode != "binary" and newline_at >= 0:
                raw = bytes(self.buffer[:newline_at]).rstrip(b"\r")
                del self.buffer[: newline_at + 1]
                if not raw.strip():
                    continue
                try:
                    text = raw.decode("utf-8", errors="strict")
                    try:
                        values = parse_serial_line(text)
                    except Exception as full_frame_error:
                        try:
                            chip_id, chip_values = parse_chip_line(text)
                        except ValueError as chip_error:
                            # Firmware banners and separator lines are useful
                            # diagnostics, but they are not malformed samples.
                            if CHIP_LINE_RE.search(text):
                                output.append(("error", f"{chip_error} | {text[:100]}"))
                            elif text.strip("-=_* .\t"):
                                output.append(("raw", text[:500]))
                            continue
                        self.chip_rows[chip_id] = chip_values
                        if len(self.chip_rows) < NUM_CHIPS:
                            continue
                        channel_ids = sorted(self.chip_rows)[:NUM_CHIPS]
                        values = np.stack([self.chip_rows[channel_id] for channel_id in channel_ids])
                        self.chip_rows.clear()
                        output.append(
                            (
                                "frame",
                                {
                                    "values": values,
                                    "sequence": None,
                                    "sensor_time": None,
                                    "source": "chip lines",
                                    "channel_ids": channel_ids,
                                    "raw": "chip frame " + ",".join(str(value) for value in channel_ids),
                                },
                            )
                        )
                        continue
                    output.append(
                        (
                            "frame",
                            {
                                "values": values,
                                "sequence": None,
                                "sensor_time": None,
                                "source": "text",
                                "raw": text,
                            },
                        )
                    )
                except Exception as exc:
                    preview = raw[:100].decode("utf-8", errors="replace")
                    output.append(("error", f"{exc} | {preview}"))
                continue

            if len(self.buffer) > MAX_BUFFER_BYTES:
                dropped = len(self.buffer) - ASKN_PACKET.size
                del self.buffer[:dropped]
                output.append(("error", f"未找到完整帧，已丢弃 {dropped} 字节"))
            break
        return output


def compute_response_scores(samples: np.ndarray, baseline: np.ndarray) -> np.ndarray:
    """Return a robust per-chip magnetic response relative to baseline."""
    values = np.asarray(samples, dtype=np.float32)
    base = np.asarray(baseline, dtype=np.float32)
    if values.ndim == 2:
        values = values[None, ...]
    if values.ndim != 3 or values.shape[1:] != (NUM_CHIPS, 4):
        raise ValueError("samples shape must be [N, 5, 4]")
    delta = np.linalg.norm(values[:, :, 1:4] - base[None, :, 1:4], axis=2)
    return np.nanmedian(delta, axis=0)


def mapping_confidence(scores: np.ndarray) -> tuple[int, float]:
    scores = np.nan_to_num(np.asarray(scores, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    order = np.argsort(scores)[::-1]
    best = int(order[0])
    first = max(float(scores[order[0]]), 0.0)
    second = max(float(scores[order[1]]), 0.0)
    confidence = (first - second) / first if first > 1e-12 else 0.0
    return best, float(np.clip(confidence, 0.0, 1.0))


class SerialReaderThread(QThread):
    frame_received = pyqtSignal(object)
    raw_received = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    status_changed = pyqtSignal(str)

    def __init__(self, port: str, baudrate: int, mode: str):
        super().__init__()
        self.port = port
        self.baudrate = int(baudrate)
        self.mode = mode
        self.running = False
        self.handle = None
        self.raw_counter = 0

    def run(self):
        if serial is None:
            self.error_occurred.emit("缺少 pyserial，请执行：pip install pyserial")
            self.status_changed.emit("Stopped")
            return
        decoder = SerialStreamDecoder(self.mode)
        self.running = True
        try:
            self.handle = serial.Serial(self.port, self.baudrate, timeout=0.15)
            self.status_changed.emit(f"Connected {self.port} @ {self.baudrate}")
            while self.running:
                waiting = int(getattr(self.handle, "in_waiting", 0))
                chunk = self.handle.read(max(1, min(waiting, 4096)))
                if not chunk:
                    continue
                for kind, payload in decoder.feed(chunk):
                    if kind == "frame":
                        payload["pc_wall_time"] = time.time()
                        payload["pc_monotonic"] = time.monotonic()
                        self.frame_received.emit(payload)
                        self.raw_counter += 1
                        if self.raw_counter % 5 == 1:
                            self.raw_received.emit(str(payload["raw"])[:500])
                    elif kind == "error":
                        self.error_occurred.emit(str(payload))
                    else:
                        self.raw_counter += 1
                        if self.raw_counter % 20 == 1:
                            self.raw_received.emit(str(payload))
        except Exception as exc:
            self.error_occurred.emit(f"串口打开/读取失败：{exc}")
        finally:
            if self.handle is not None:
                try:
                    self.handle.close()
                except Exception:
                    pass
                self.handle = None
            self.running = False
            self.status_changed.emit("Stopped")

    def stop(self):
        self.running = False
        if self.handle is not None:
            try:
                self.handle.cancel_read()
            except Exception:
                pass


class MetricCard(QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("MetricCard")
        self.setMinimumHeight(82)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("MetricTitle")
        self.value_label = QLabel("--")
        font = QFont()
        font.setPointSize(17)
        font.setBold(True)
        self.value_label.setFont(font)
        self.detail_label = QLabel("")
        self.detail_label.setObjectName("MetricDetail")
        layout.addWidget(title_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.detail_label)

    def set_metric(self, value: str, detail: str = "", color: str = "#2563eb"):
        self.value_label.setText(value)
        self.value_label.setStyleSheet(f"color: {color};")
        self.detail_label.setText(detail)


class SerialMapperWindow(QMainWindow):
    def __init__(self, port: str = "COM9", baudrate: int = 115200, auto_start: bool = False):
        super().__init__()
        app = QApplication.instance()
        if app is not None and sys.platform.startswith("win"):
            # Qt's offscreen and some packaged runtimes do not automatically
            # discover Windows CJK fonts even though they are installed.
            windows_font = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc"
            if windows_font.exists():
                QFontDatabase.addApplicationFont(str(windows_font))
            app.setFont(QFont("Microsoft YaHei UI", 9))
        self.setWindowTitle("磁传感器串口对应关系工具")
        self.resize(1500, 920)
        self.setMinimumSize(1120, 760)
        self.reader: Optional[SerialReaderThread] = None
        self.csv_file = None
        self.csv_writer = None
        self.csv_path = None
        self.last_csv_flush = 0.0
        self.reset_state()
        self.build_ui(port, baudrate)
        self.apply_styles()
        self.refresh_ports(preferred=port)

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self.refresh_ui)
        self.ui_timer.start(150)
        if auto_start:
            QTimer.singleShot(150, self.start_reader)

    def reset_state(self):
        self.started_at = time.monotonic()
        self.frames = 0
        self.errors = 0
        self.latest_frame = None
        self.history = deque(maxlen=MAX_HISTORY_POINTS)
        self.packet_times = deque()
        self.baseline = None
        self.baseline_at = None
        self.mapping = {}
        self.auto_baseline_done = False
        if not hasattr(self, "channel_labels"):
            self.channel_labels = [f"S{index}" for index in range(NUM_CHIPS)]

    def build_ui(self, port: str, baudrate: int):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        heading = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel("磁传感器串口对应关系")
        title.setObjectName("WindowTitle")
        subtitle = QLabel("把磁铁依次靠近 5 个物理位置，用相对基线响应锁定 COM 通道")
        subtitle.setObjectName("Subtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        heading.addLayout(titles)
        heading.addStretch()
        root.addLayout(heading)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("串口"))
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(150)
        self.port_combo.setEditText(port)
        controls.addWidget(self.port_combo)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh_ports)
        controls.addWidget(self.refresh_button)
        controls.addWidget(QLabel("波特率"))
        self.baud_combo = QComboBox()
        for value in (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600):
            self.baud_combo.addItem(str(value), value)
        index = self.baud_combo.findData(baudrate)
        self.baud_combo.setCurrentIndex(max(index, 0))
        controls.addWidget(self.baud_combo)
        controls.addWidget(QLabel("格式"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("自动（文本 / ASKN）", "auto")
        self.mode_combo.addItem("文本行", "text")
        self.mode_combo.addItem("ASKN 96 字节", "binary")
        controls.addWidget(self.mode_combo)
        self.auto_baseline_check = QCheckBox("连接后自动基线")
        self.auto_baseline_check.setChecked(True)
        controls.addWidget(self.auto_baseline_check)
        self.save_csv_check = QCheckBox("保存 CSV")
        controls.addWidget(self.save_csv_check)
        self.start_button = QPushButton("连接 COM9")
        self.start_button.clicked.connect(self.start_reader)
        controls.addWidget(self.start_button)
        self.stop_button = QPushButton("断开")
        self.stop_button.clicked.connect(self.stop_reader)
        self.stop_button.setEnabled(False)
        controls.addWidget(self.stop_button)
        controls.addStretch()
        root.addLayout(controls)

        cards = QHBoxLayout()
        self.status_card = MetricCard("连接")
        self.rate_card = MetricCard("采样率")
        self.strongest_card = MetricCard("最强响应")
        self.baseline_card = MetricCard("基线")
        for card in (self.status_card, self.rate_card, self.strongest_card, self.baseline_card):
            cards.addWidget(card)
        root.addLayout(cards)

        middle = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(section_label("实时数值"))
        self.value_table = QTableWidget(NUM_CHIPS, 7)
        self.value_table.setHorizontalHeaderLabels(["通道", "T", "X", "Y", "Z", "|B|", "Δ基线"])
        self.value_table.verticalHeader().setVisible(False)
        self.value_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.value_table.setMaximumWidth(560)
        for row in range(NUM_CHIPS):
            for col in range(7):
                text = self.channel_labels[row] if col == 0 else "--"
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter if col == 0 else Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.value_table.setItem(row, col, item)
        left.addWidget(self.value_table)

        left.addWidget(section_label("当前响应（相对无磁铁基线）"))
        if pg is None:
            self.response_plot = QLabel("未安装 pyqtgraph；表格和映射功能仍可使用")
            self.response_plot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        else:
            pg.setConfigOptions(antialias=True)
            self.response_plot = pg.PlotWidget()
            self.response_plot.setBackground("w")
            self.response_plot.setLabel("left", "ΔB")
            self.response_plot.getAxis("bottom").setTicks(
                [[(i, self.channel_labels[i]) for i in range(NUM_CHIPS)]]
            )
            self.response_plot.showGrid(y=True, alpha=0.2)
            self.response_plot.setMouseEnabled(x=False, y=True)
        left.addWidget(self.response_plot, 1)
        middle.addLayout(left, 5)

        right = QVBoxLayout()
        plot_head = QHBoxLayout()
        plot_head.addWidget(section_label("5 通道响应趋势"))
        plot_head.addStretch()
        plot_head.addWidget(QLabel("三轴通道"))
        self.sensor_combo = QComboBox()
        for index in range(NUM_CHIPS):
            self.sensor_combo.addItem(self.channel_labels[index], index)
        plot_head.addWidget(self.sensor_combo)
        right.addLayout(plot_head)
        if pg is None:
            self.trend_plot = QLabel("")
            self.axes_plot = QLabel("")
            self.trend_curves = []
            self.axis_curves = []
        else:
            self.trend_plot = pg.PlotWidget()
            self.trend_plot.setBackground("w")
            self.trend_plot.setLabel("left", "ΔB")
            self.trend_plot.setLabel("bottom", "最近时间", units="s")
            self.trend_plot.showGrid(x=True, y=True, alpha=0.2)
            self.trend_legend = self.trend_plot.addLegend()
            self.trend_plot.setMouseEnabled(x=False, y=True)
            self.trend_curves = [
                self.trend_plot.plot([], [], pen=pg.mkPen(SERIES_COLORS[i], width=2), name=f"S{i}")
                for i in range(NUM_CHIPS)
            ]
            self.axes_plot = pg.PlotWidget()
            self.axes_plot.setBackground("w")
            self.axes_plot.setLabel("left", "磁场")
            self.axes_plot.setLabel("bottom", "最近时间", units="s")
            self.axes_plot.showGrid(x=True, y=True, alpha=0.2)
            self.axes_plot.addLegend()
            self.axes_plot.setMouseEnabled(x=False, y=True)
            self.axis_curves = [
                self.axes_plot.plot([], [], pen=pg.mkPen(color, width=2), name=axis)
                for color, axis in zip(("#ef4444", "#16a34a", "#2563eb"), ("X", "Y", "Z"))
            ]
        right.addWidget(self.trend_plot, 1)
        right.addWidget(self.axes_plot, 1)
        middle.addLayout(right, 8)
        root.addLayout(middle, 1)

        mapping_row = QHBoxLayout()
        mapping_controls = QVBoxLayout()
        mapping_controls.addWidget(section_label("记录物理位置 → 串口通道"))
        position_row = QHBoxLayout()
        self.position_combo = QComboBox()
        for index in range(NUM_CHIPS):
            self.position_combo.addItem(f"物理位置 P{index}", index)
        position_row.addWidget(self.position_combo)
        self.baseline_button = QPushButton("设置无磁铁基线")
        self.baseline_button.clicked.connect(self.capture_baseline)
        position_row.addWidget(self.baseline_button)
        self.record_button = QPushButton("记录当前对应")
        self.record_button.clicked.connect(self.record_mapping)
        position_row.addWidget(self.record_button)
        self.clear_button = QPushButton("清空映射")
        self.clear_button.clicked.connect(self.clear_mapping)
        position_row.addWidget(self.clear_button)
        mapping_controls.addLayout(position_row)
        hint = QLabel("操作：先让磁铁远离并设基线；再靠近选定物理位置，等柱状图稳定后记录。建议置信度 ≥ 40%。")
        hint.setObjectName("Subtitle")
        hint.setWordWrap(True)
        mapping_controls.addWidget(hint)
        mapping_row.addLayout(mapping_controls, 5)

        self.mapping_table = QTableWidget(NUM_CHIPS, 4)
        self.mapping_table.setHorizontalHeaderLabels(["物理位置", "串口通道", "响应", "置信度"])
        self.mapping_table.verticalHeader().setVisible(False)
        self.mapping_table.verticalHeader().setDefaultSectionSize(24)
        self.mapping_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.mapping_table.setMaximumHeight(165)
        for row in range(NUM_CHIPS):
            for col, value in enumerate((f"P{row}", "--", "--", "--")):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.mapping_table.setItem(row, col, item)
        mapping_row.addWidget(self.mapping_table, 4)

        raw_layout = QVBoxLayout()
        raw_layout.addWidget(section_label("原始数据 / 解析信息"))
        self.raw_log = QPlainTextEdit()
        self.raw_log.setReadOnly(True)
        self.raw_log.setMaximumHeight(135)
        self.raw_log.document().setMaximumBlockCount(120)
        raw_layout.addWidget(self.raw_log)
        mapping_row.addLayout(raw_layout, 5)
        root.addLayout(mapping_row)

    def refresh_ports(self, checked=False, preferred: Optional[str] = None):
        current = preferred or self.port_combo.currentText().strip() or "COM9"
        self.port_combo.clear()
        available = []
        if list_ports is not None:
            try:
                available = list(list_ports.comports())
            except Exception as exc:
                self.log(f"端口枚举失败：{exc}")
        names = [item.device for item in available]
        if current not in names:
            names.insert(0, current)
        if "COM9" not in names:
            names.append("COM9")
        for name in names:
            description = next((p.description for p in available if p.device == name), "手动指定")
            self.port_combo.addItem(f"{name} — {description}", name)
        selected = self.port_combo.findData(current)
        self.port_combo.setCurrentIndex(max(selected, 0))

    def selected_port(self) -> str:
        data = self.port_combo.currentData()
        if data:
            return str(data)
        return self.port_combo.currentText().split("—", 1)[0].strip()

    def start_reader(self):
        if self.reader is not None:
            return
        if serial is None:
            QMessageBox.critical(self, "缺少依赖", "未安装 pyserial。请执行：\npip install pyserial")
            return
        port = self.selected_port() or "COM9"
        baudrate = int(self.baud_combo.currentData())
        mode = str(self.mode_combo.currentData())
        self.close_csv()
        self.reset_state()
        if self.save_csv_check.isChecked():
            try:
                self.open_csv()
            except Exception as exc:
                QMessageBox.critical(self, "CSV 错误", str(exc))
                return
        self.reader = SerialReaderThread(port, baudrate, mode)
        self.reader.frame_received.connect(self.on_frame)
        self.reader.raw_received.connect(lambda message: self.log(f"RX {message}"))
        self.reader.error_occurred.connect(self.on_error)
        self.reader.status_changed.connect(self.on_status)
        self.reader.start()
        self.set_running(True)
        self.log(f"正在打开 {port}，波特率 {baudrate}，模式 {mode}")

    def stop_reader(self):
        if self.reader is not None:
            self.reader.stop()
            self.reader.wait(1500)
            self.reader = None
        self.close_csv()
        self.set_running(False)

    def on_status(self, message: str):
        self.log(message)
        if message == "Stopped":
            self.reader = None
            self.close_csv()
            self.set_running(False)

    def on_error(self, message: str):
        self.errors += 1
        if self.errors <= 8 or self.errors % 50 == 0:
            self.log(f"解析错误 #{self.errors}: {message}")

    def on_frame(self, frame: dict):
        values = np.asarray(frame["values"], dtype=np.float32)
        if frame.get("channel_ids") is not None:
            self.set_channel_ids(frame["channel_ids"])
        now = float(frame["pc_monotonic"])
        self.latest_frame = frame
        self.frames += 1
        elapsed = now - self.started_at
        self.history.append((elapsed, values.copy()))
        self.packet_times.append(now)
        while self.packet_times and now - self.packet_times[0] > 3.0:
            self.packet_times.popleft()
        self.update_value_table(values)
        self.write_csv(frame, values)
        if (
            self.auto_baseline_check.isChecked()
            and not self.auto_baseline_done
            and self.frames >= 30
            and elapsed >= 0.8
        ):
            self.capture_baseline(quiet=True)
            self.auto_baseline_done = True

    def recent_values(self, seconds: float = 1.0) -> Optional[np.ndarray]:
        if not self.history:
            return None
        latest = self.history[-1][0]
        rows = [values for timestamp, values in self.history if timestamp >= latest - seconds]
        return np.stack(rows) if rows else None

    def capture_baseline(self, checked=False, quiet: bool = False):
        recent = self.recent_values(1.2)
        if recent is None or len(recent) < 5:
            if not quiet:
                QMessageBox.information(self, "等待数据", "至少需要 5 帧有效数据才能设置基线。")
            return
        with np.errstate(invalid="ignore"):
            self.baseline = np.nanmedian(recent, axis=0).astype(np.float32)
        self.baseline_at = time.monotonic()
        if not quiet:
            self.log(f"已用最近 {len(recent)} 帧设置无磁铁基线")

    def current_scores(self) -> np.ndarray:
        if self.baseline is None:
            return np.zeros(NUM_CHIPS, dtype=float)
        recent = self.recent_values(0.45)
        if recent is None:
            return np.zeros(NUM_CHIPS, dtype=float)
        return compute_response_scores(recent, self.baseline)

    def record_mapping(self):
        if self.baseline is None:
            QMessageBox.information(self, "尚无基线", "请先让磁铁远离传感器，然后点击“设置无磁铁基线”。")
            return
        recent = self.recent_values(0.8)
        if recent is None or len(recent) < 5:
            QMessageBox.information(self, "等待数据", "请等待响应稳定后再记录。")
            return
        scores = compute_response_scores(recent, self.baseline)
        channel, confidence = mapping_confidence(scores)
        position = int(self.position_combo.currentData())
        duplicate_positions = [
            mapped_position
            for mapped_position, entry in self.mapping.items()
            if mapped_position != position and entry["channel"] == channel
        ]
        self.mapping[position] = {
            "channel": channel,
            "score": float(scores[channel]),
            "confidence": confidence,
        }
        self.update_mapping_table()
        self.log(
            f"映射 P{position} → {self.channel_labels[channel]}，响应 {scores[channel]:.3f}，置信度 {confidence * 100:.0f}%"
        )
        if duplicate_positions:
            joined = ", ".join(f"P{value}" for value in duplicate_positions)
            self.log(
                f"注意：{self.channel_labels[channel]} 也已分配给 {joined}；"
                "请增大磁铁与相邻传感器的距离后复测"
            )
        for offset in range(1, NUM_CHIPS + 1):
            candidate = (position + offset) % NUM_CHIPS
            if candidate not in self.mapping:
                self.position_combo.setCurrentIndex(candidate)
                break

    def clear_mapping(self):
        self.mapping.clear()
        self.update_mapping_table()
        self.log("映射已清空")

    def update_mapping_table(self):
        for position in range(NUM_CHIPS):
            entry = self.mapping.get(position)
            values = (
                (
                    self.channel_labels[entry["channel"]],
                    f"{entry['score']:.3f}",
                    f"{entry['confidence'] * 100:.0f}%",
                )
                if entry
                else ("--", "--", "--")
            )
            for col, value in enumerate(values, start=1):
                self.mapping_table.item(position, col).setText(value)

    def update_value_table(self, values: np.ndarray):
        if self.baseline is None:
            scores = np.zeros(NUM_CHIPS)
        else:
            scores = np.linalg.norm(values[:, 1:4] - self.baseline[:, 1:4], axis=1)
        magnitudes = np.linalg.norm(values[:, 1:4], axis=1)
        for row in range(NUM_CHIPS):
            display = [values[row, 0], *values[row, 1:4], magnitudes[row], scores[row]]
            for col, value in enumerate(display, start=1):
                text = "--" if not math.isfinite(float(value)) else f"{float(value):.3f}"
                self.value_table.item(row, col).setText(text)

    def set_channel_ids(self, channel_ids):
        labels = [f"chip {int(channel_id)}" for channel_id in channel_ids]
        if len(labels) != NUM_CHIPS or labels == self.channel_labels:
            return
        self.channel_labels = labels
        for index, label in enumerate(labels):
            self.value_table.item(index, 0).setText(label)
            self.sensor_combo.setItemText(index, label)
        if pg is not None:
            self.response_plot.getAxis("bottom").setTicks(
                [[(index, label) for index, label in enumerate(labels)]]
            )
            self.trend_legend.clear()
            for curve, label in zip(self.trend_curves, labels):
                self.trend_legend.addItem(curve, label)

    def refresh_ui(self):
        connected = self.reader is not None
        if connected and self.latest_frame is not None:
            source = self.latest_frame.get("source", "")
            self.status_card.set_metric("接收中", f"{self.selected_port()} · {source}", "#16a34a")
        elif connected:
            self.status_card.set_metric("已连接", "等待有效帧", "#2563eb")
        else:
            self.status_card.set_metric("未连接", "默认 COM9", "#64748b")

        if len(self.packet_times) >= 2:
            rate = (len(self.packet_times) - 1) / max(self.packet_times[-1] - self.packet_times[0], 1e-6)
        else:
            rate = 0.0
        self.rate_card.set_metric(f"{rate:.1f} Hz", f"有效 {self.frames:,} · 错误 {self.errors:,}", "#2563eb")

        scores = self.current_scores()
        channel, confidence = mapping_confidence(scores)
        if self.baseline is None:
            self.strongest_card.set_metric("--", "等待基线", "#64748b")
            self.baseline_card.set_metric("未设置", "移开磁铁后点击设置", "#f59e0b")
        else:
            self.strongest_card.set_metric(
                self.channel_labels[channel],
                f"Δ={scores[channel]:.3f} · 区分度 {confidence * 100:.0f}%",
                SERIES_COLORS[channel],
            )
            age = max(time.monotonic() - self.baseline_at, 0.0)
            self.baseline_card.set_metric("已设置", f"{age:.0f} 秒前", "#16a34a")
        self.refresh_plots(scores)

    def refresh_plots(self, scores: np.ndarray):
        if pg is None:
            return
        self.response_plot.clear()
        brushes = [pg.mkBrush(color) for color in SERIES_COLORS]
        bar = pg.BarGraphItem(x=np.arange(NUM_CHIPS), height=scores, width=0.68, brushes=brushes)
        self.response_plot.addItem(bar)
        self.response_plot.setYRange(0, max(float(np.max(scores)) * 1.18, 1.0), padding=0)
        if not self.history or self.baseline is None:
            return
        latest = self.history[-1][0]
        rows = [(timestamp, values) for timestamp, values in self.history if timestamp >= latest - 8.0]
        if not rows:
            return
        if len(rows) > 800:
            step = max(len(rows) // 800, 1)
            rows = rows[::step]
        times = np.asarray([row[0] for row in rows], dtype=float)
        values = np.stack([row[1] for row in rows])
        times -= times[-1]
        deltas = np.linalg.norm(values[:, :, 1:4] - self.baseline[None, :, 1:4], axis=2)
        for index, curve in enumerate(self.trend_curves):
            curve.setData(times, deltas[:, index])
        selected = int(self.sensor_combo.currentData())
        for axis, curve in enumerate(self.axis_curves, start=1):
            curve.setData(times, values[:, selected, axis])
        self.trend_plot.setXRange(-8.0, 0.0, padding=0)
        self.axes_plot.setXRange(-8.0, 0.0, padding=0)

    def open_csv(self):
        folder = Path.cwd() / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        self.csv_path = folder / f"anyskin_serial_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        self.csv_file = self.csv_path.open("w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        header = ["pc_receive_time", "sequence", "sensor_time"]
        for index in range(NUM_CHIPS):
            header.extend([f"s{index}_t", f"s{index}_x", f"s{index}_y", f"s{index}_z"])
        self.csv_writer.writerow(header)
        self.last_csv_flush = time.monotonic()
        self.log(f"CSV: {self.csv_path}")

    def write_csv(self, frame: dict, values: np.ndarray):
        if self.csv_writer is None:
            return
        row = [frame["pc_wall_time"], frame.get("sequence"), frame.get("sensor_time")]
        row.extend(values.reshape(-1).tolist())
        self.csv_writer.writerow(row)
        if time.monotonic() - self.last_csv_flush >= 1.0:
            self.csv_file.flush()
            self.last_csv_flush = time.monotonic()

    def close_csv(self):
        if self.csv_file is not None:
            try:
                self.csv_file.flush()
                self.csv_file.close()
            finally:
                self.csv_file = None
                self.csv_writer = None

    def set_running(self, running: bool):
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.port_combo.setEnabled(not running)
        self.refresh_button.setEnabled(not running)
        self.baud_combo.setEnabled(not running)
        self.mode_combo.setEnabled(not running)
        self.save_csv_check.setEnabled(not running)
        self.start_button.setText(f"连接 {self.selected_port() or 'COM9'}")

    def log(self, message: str):
        self.raw_log.appendPlainText(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def closeEvent(self, event):
        self.stop_reader()
        event.accept()

    def apply_styles(self):
        self.setStyleSheet(
            """
            QWidget { background: #f8fafc; color: #0f172a; font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"; font-size: 13px; }
            QLabel#WindowTitle { font-size: 25px; font-weight: 700; }
            QLabel#Subtitle, QLabel#MetricDetail { color: #64748b; }
            QLabel#SectionLabel { color: #1e293b; font-weight: 700; }
            QFrame#MetricCard { background: white; border: 1px solid #dbe3ef; border-radius: 8px; }
            QLabel#MetricTitle { color: #64748b; font-weight: 600; }
            QPushButton { background: #2563eb; color: white; border: none; border-radius: 6px; padding: 7px 11px; font-weight: 600; }
            QPushButton:disabled { background: #94a3b8; }
            QComboBox, QLineEdit, QSpinBox, QTableWidget, QPlainTextEdit { background: white; border: 1px solid #cbd5e1; border-radius: 6px; padding: 4px; }
            QHeaderView::section { background: #e2e8f0; color: #0f172a; padding: 5px; border: 1px solid #cbd5e1; font-weight: 700; }
            """
        )


def section_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SectionLabel")
    return label


def main() -> int:
    parser = argparse.ArgumentParser(description="Visualize COM magnetic data and map five physical sensors")
    parser.add_argument("--port", default="COM9", help="Serial port (default: COM9)")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate")
    parser.add_argument("--auto-start", action="store_true", help="Open the serial port on launch")
    args = parser.parse_args()
    app = QApplication(sys.argv)
    window = SerialMapperWindow(args.port, args.baud, args.auto_start)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
