from __future__ import annotations

import argparse
import csv
import json
import math
import os
import socket
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path

try:
    import av
except Exception:
    av = None

os.environ.setdefault("QT_API", "pyqt6")
PYQTGRAPH_IMPORT_ERROR = ""
try:
    import pyqtgraph as pg
except Exception as exc:
    pg = None
    PYQTGRAPH_IMPORT_ERROR = str(exc)

from PyQt6.QtCore import QObject, QThread, QTimer, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QImage, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from capture_upload_server import create_upload_server, get_default_upload_dir
from experiment_data import ExperimentDataset, TimedRows, discover_experiments
from offline_gripper_processor import (
    INTRINSICS_NAME,
    STATE_NAME as GRIPPER_STATE_NAME,
    OfflineGripperProcessor,
    intrinsics_to_dict,
    save_ultrawide_intrinsics,
)
from pose_magnetic_receiver import APM1DecodeError, decode_apm1_packet
from udp_video_debug_ui import LatencyClockCompensator, VideoReceiverThread, get_app_base_dir
from aruco_config_ui import ArucoConfigWidget


RECEIVER_FIELDS = [
    "kind",
    "identifier",
    "sender_time",
    "pc_receive_time",
    "pc_decode_time",
    "experiment_time",
    "raw_latency_ms",
    "corrected_latency_ms",
    "clock_offset_ms",
    "fps",
    "bitrate_mbps",
    "dropped_frames",
    "packets",
    "bytes",
]

PC_GENERATED_EXPERIMENT_FILES = {
    "experiment_state.json",
    "receiver_transport.csv",
    "upload_state.json",
    "zarr_state.json",
    GRIPPER_STATE_NAME,
}


def acquire_single_instance_mutex() -> tuple[bool, object | None]:
    """Prevent hidden/duplicate Windows monitors from sharing the UDP ports."""
    if sys.platform != "win32":
        return True, None
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.CreateMutexW(None, False, "Local\\ARPoseExperimentMonitor")
    if not handle:
        return True, None
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False, None
    return True, (kernel32, handle)


def release_single_instance_mutex(mutex: object | None) -> None:
    if mutex is None:
        return
    kernel32, handle = mutex
    kernel32.CloseHandle(handle)


def metric_text(value: object, suffix: str = "", precision: int = 1) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    if not math.isfinite(number):
        return "--"
    return f"{number:.{precision}f}{suffix}"


def file_size_text(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return "--"


class UploadServerBridge(QObject):
    event_received = pyqtSignal(dict)
    status_changed = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.server = None
        self.thread: threading.Thread | None = None
        self.last_error = ""

    def start(self, host: str, port: int, root: Path) -> bool:
        if self.server is not None:
            return True
        self.last_error = ""
        try:
            self.server = create_upload_server(host, port, root, self.event_received.emit)
        except Exception as exc:
            self.server = None
            error_log = root / "upload_server_error.log"
            try:
                error_log.write_text(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n{traceback.format_exc()}",
                    encoding="utf-8",
                )
                self.last_error = f"{exc} (details: {error_log})"
            except OSError:
                self.last_error = str(exc)
            self.status_changed.emit(f"Upload bind failed: {exc}")
            return False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.status_changed.emit(f"Upload server {host}:{port}")
        return True

    def stop(self) -> None:
        server = self.server
        self.server = None
        if server is not None:
            server.shutdown()
            server.server_close()
        self.thread = None
        self.status_changed.emit("Upload server stopped")


class CombinedReceiverThread(QThread):
    metrics_ready = pyqtSignal(dict)
    status_changed = pyqtSignal(str)

    def __init__(
        self,
        bind_host: str,
        port: int,
        phone_ip: str,
        registration_port: int,
        video_port: int,
    ) -> None:
        super().__init__()
        self.bind_host = bind_host
        self.port = port
        self.phone_ip = phone_ip
        self.registration_port = registration_port
        self.video_port = video_port
        self.running = True
        self.clock = LatencyClockCompensator()
        self.packet_times: deque[float] = deque()

    def stop(self) -> None:
        self.running = False

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.bind_host, self.port))
        except OSError as exc:
            self.status_changed.emit(f"Combined bind failed: {exc}")
            sock.close()
            return
        sock.settimeout(0.2)
        hello = f"PC_HELLO,1,{self.port},{self.video_port}\n".encode("ascii")
        next_hello = 0.0
        self.status_changed.emit(f"Combined sensor listening {self.bind_host}:{self.port}")

        try:
            while self.running:
                now = time.monotonic()
                if self.phone_ip and now >= next_hello:
                    try:
                        sock.sendto(hello, (self.phone_ip, self.registration_port))
                    except OSError:
                        pass
                    next_hello = now + 2.0
                try:
                    datagram, address = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break

                receive_wall = time.time()
                receive_mono = time.monotonic()
                try:
                    packet = decode_apm1_packet(datagram)
                except APM1DecodeError:
                    continue

                latency = self.clock.observe(
                    packet.phone_send_unix,
                    receive_wall,
                    receive_mono,
                    is_pose_reference=True,
                )
                self.packet_times.append(receive_mono)
                while self.packet_times and receive_mono - self.packet_times[0] > 1.0:
                    self.packet_times.popleft()
                offset = self.clock.offset_seconds
                latest_magnetic = packet.magnetic_samples[-1] if packet.magnetic_samples else None
                metrics = {
                    "status": f"Combined from {address[0]}:{address[1]}",
                    "session_id": str(packet.session_id),
                    "packet_sequence": packet.packet_sequence,
                    "sender_timestamp": packet.phone_send_unix,
                    "receive_wall_time": receive_wall,
                    "raw_latency_ms": (receive_wall - packet.phone_send_unix) * 1000.0,
                    "latency_ms": latency,
                    "clock_offset_ms": offset * 1000.0 if offset is not None else None,
                    "fps": float(len(self.packet_times)),
                    "magnetic_count": len(packet.magnetic_samples),
                    "chips": latest_magnetic.sensors() if latest_magnetic else (),
                    "magnetic_sequence": latest_magnetic.sequence if latest_magnetic else None,
                }
                self.metrics_ready.emit(metrics)
        finally:
            sock.close()
            self.status_changed.emit("Combined sensor stopped")


class ReceiverDiagnosticsRecorder:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.pre_roll: deque[tuple[float, dict[str, object]]] = deque()
        self.active: dict[str, dict[str, object]] = {}

    def set_root(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def record(self, row: dict[str, object]) -> None:
        now = time.monotonic()
        normalized = {field: row.get(field, "") for field in RECEIVER_FIELDS}
        self.pre_roll.append((now, normalized))
        while self.pre_roll and now - self.pre_roll[0][0] > 5.0:
            self.pre_roll.popleft()
        for session in self.active.values():
            self._write_row(session, normalized)

    def handle_control(self, event: dict) -> None:
        experiment_id = str(event.get("experiment_id", ""))
        if not experiment_id:
            return
        if event.get("event") == "start":
            self._start(
                experiment_id,
                float(event.get("event_unix_time", 0.0)),
                Path(str(event.get("directory", self.root / experiment_id))),
            )
        elif event.get("event") == "stop":
            self._stop(experiment_id, float(event.get("event_unix_time", 0.0)))

    def _start(self, experiment_id: str, start_unix: float, directory: Path) -> None:
        if experiment_id in self.active:
            return
        directory.mkdir(parents=True, exist_ok=True)
        part_path = directory / "receiver_transport.part.csv"
        handle = part_path.open("w", newline="", encoding="utf-8")
        writer = csv.DictWriter(handle, fieldnames=RECEIVER_FIELDS)
        writer.writeheader()
        session: dict[str, object] = {
            "directory": directory,
            "part_path": part_path,
            "handle": handle,
            "writer": writer,
            "start_unix": start_unix,
        }
        self.active[experiment_id] = session
        for _, row in self.pre_roll:
            sender_time = _safe_float(row.get("sender_time"))
            if sender_time >= start_unix:
                self._write_row(session, row)

    def _stop(self, experiment_id: str, stop_unix: float) -> None:
        session = self.active.pop(experiment_id, None)
        if session is None:
            return
        handle = session["handle"]
        handle.close()
        part_path = Path(session["part_path"])
        target_path = Path(session["directory"]) / "receiver_transport.csv"
        start_unix = float(session["start_unix"])
        with part_path.open("r", newline="", encoding="utf-8") as source, target_path.open(
            "w", newline="", encoding="utf-8"
        ) as target:
            reader = csv.DictReader(source)
            writer = csv.DictWriter(target, fieldnames=RECEIVER_FIELDS)
            writer.writeheader()
            for row in reader:
                sender_time = _safe_float(row.get("sender_time"))
                if start_unix <= sender_time <= stop_unix:
                    row["experiment_time"] = f"{sender_time - start_unix:.9f}"
                    writer.writerow(row)
        part_path.unlink(missing_ok=True)
        self._register_component(Path(session["directory"]), target_path.name)

    @staticmethod
    def _write_row(session: dict[str, object], row: dict[str, object]) -> None:
        writer = session["writer"]
        writer.writerow(row)
        session["handle"].flush()

    @staticmethod
    def _register_component(directory: Path, filename: str) -> None:
        state_path = directory / "upload_state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        components = state.get("components") if isinstance(state.get("components"), dict) else {}
        components["receiver_transport"] = filename
        state["components"] = components
        temp = state_path.with_suffix(".json.part")
        temp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(state_path)

    def close(self) -> None:
        for experiment_id in list(self.active):
            self._stop(experiment_id, time.time())


class VideoFilePlayback:
    def __init__(self) -> None:
        self.container = None
        self.stream = None
        self.iterator = None
        self.current: tuple[float, QImage] | None = None
        self.next_frame: tuple[float, QImage] | None = None
        self.last_target = -1.0

    def open(self, path: Path | None) -> None:
        self.close()
        if av is None or path is None or not path.is_file():
            return
        self.container = av.open(str(path))
        self.stream = self.container.streams.video[0]
        self.iterator = iter(self.container.decode(self.stream))
        self.next_frame = self._decode_next()

    def close(self) -> None:
        if self.container is not None:
            self.container.close()
        self.container = None
        self.stream = None
        self.iterator = None
        self.current = None
        self.next_frame = None
        self.last_target = -1.0

    def frame_at(self, target: float) -> QImage | None:
        if self.container is None or self.stream is None or target < 0:
            return None
        if target + 0.05 < self.last_target or target - self.last_target > 1.5:
            timestamp = max(0, int(target / float(self.stream.time_base)))
            self.container.seek(timestamp, stream=self.stream, backward=True, any_frame=False)
            self.iterator = iter(self.container.decode(self.stream))
            self.current = None
            self.next_frame = self._decode_next()
        self.last_target = target
        while self.next_frame is not None and self.next_frame[0] <= target:
            self.current = self.next_frame
            self.next_frame = self._decode_next()
        return self.current[1] if self.current is not None else (self.next_frame[1] if self.next_frame else None)

    def _decode_next(self) -> tuple[float, QImage] | None:
        if self.iterator is None:
            return None
        try:
            frame = next(self.iterator)
        except StopIteration:
            return None
        frame_time = float(frame.time or 0.0)
        rgb = frame.to_ndarray(format="rgb24")
        height, width, channels = rgb.shape
        image = QImage(rgb.data, width, height, channels * width, QImage.Format.Format_RGB888).copy()
        return frame_time, image


class ExperimentMonitorWindow(QMainWindow):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()
        self.setWindowTitle("ARPose Experiment Monitor & Replay")
        self.resize(1550, 980)
        self.args = args
        self.video_worker: VideoReceiverThread | None = None
        self.aruco_video_worker: VideoReceiverThread | None = None
        self.combined_worker: CombinedReceiverThread | None = None
        self.upload_bridge = UploadServerBridge()
        self.upload_bridge.event_received.connect(self.on_server_event)
        self.upload_bridge.status_changed.connect(self.set_service_status)
        self.diagnostics = ReceiverDiagnosticsRecorder(Path(args.experiments))
        self.offline_gripper_processor = OfflineGripperProcessor()
        self.ultrawide_intrinsics_path = Path(args.aruco_config).resolve().parent / INTRINSICS_NAME
        self.last_ultrawide_intrinsics_values: dict | None = None
        self.offline_backfill_started = False
        self.datasets: list[ExperimentDataset] = []
        self.dataset: ExperimentDataset | None = None
        self.playback = VideoFilePlayback()
        self.playing = False
        self.play_time = 0.0
        self.last_tick = time.monotonic()
        self.last_video_id = -1
        self.last_ultrawide_video_id = -1
        self.last_pose_id = -1
        self.last_combined_id = -1
        self.live_video_frames: dict[str, QImage] = {}
        self.live_video_metrics_by_camera: dict[str, dict] = {}
        self.plot_cursors = []
        self._build_ui()
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        QTimer.singleShot(0, self.start_services)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        service_box = QGroupBox("Monitoring Services")
        service_grid = QGridLayout(service_box)
        self.bind_edit = QLineEdit(self.args.bind)
        self.video_port_edit = QLineEdit(str(self.args.video_port))
        self.aruco_video_port_edit = QLineEdit(str(self.args.aruco_video_port))
        self.pose_port_edit = QLineEdit(str(self.args.pose_port))
        self.combined_port_edit = QLineEdit(str(self.args.combined_port))
        self.upload_port_edit = QLineEdit(str(self.args.upload_port))
        self.phone_ip_edit = QLineEdit(self.args.phone_ip)
        self.root_edit = QLineEdit(str(Path(self.args.experiments).resolve()))
        for column, (label, widget) in enumerate(
            [
                ("Bind", self.bind_edit),
                ("1x Video", self.video_port_edit),
                ("0.5x ArUco", self.aruco_video_port_edit),
                ("Pose", self.pose_port_edit),
                ("Combined", self.combined_port_edit),
                ("Upload", self.upload_port_edit),
                ("Phone IP", self.phone_ip_edit),
            ]
        ):
            service_grid.addWidget(QLabel(label), 0, column)
            service_grid.addWidget(widget, 1, column)
        service_grid.addWidget(QLabel("Experiment Library"), 2, 0)
        service_grid.addWidget(self.root_edit, 2, 1, 1, 5)
        self.services_button = QPushButton("Start Monitor")
        self.services_button.clicked.connect(self.toggle_services)
        service_grid.addWidget(self.services_button, 2, 6)
        self.upload_health = QLabel("Upload storage: not started")
        self.upload_health.setWordWrap(True)
        self.upload_health.setStyleSheet("color:#616161; font-weight:700;")
        service_grid.addWidget(self.upload_health, 3, 0, 1, 7)
        self.service_status = QLabel("Stopped")
        self.service_status.setWordWrap(True)
        service_grid.addWidget(self.service_status, 4, 0, 1, 7)
        root.addWidget(service_box)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_live_tab(), "Live Monitor")
        self.aruco_panel = ArucoConfigWidget(Path(self.args.aruco_config))
        self.aruco_panel.apply_requested.connect(self.apply_aruco_configuration)
        self.tabs.addTab(self.aruco_panel, "ArUco Gripper")
        self.tabs.addTab(self._build_replay_tab(), "Experiment Replay")
        root.addWidget(self.tabs, 1)

    def _build_live_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        camera_view = QVBoxLayout()
        camera_selector = QHBoxLayout()
        camera_selector.addWidget(QLabel("Camera view"))
        self.live_camera_combo = QComboBox()
        self.live_camera_combo.addItem("1× Main (ARKit)", "main")
        self.live_camera_combo.addItem("0.5× Ultra-wide (ArUco)", "ultrawide")
        self.live_camera_combo.currentIndexChanged.connect(self.change_live_camera)
        camera_selector.addWidget(self.live_camera_combo)
        camera_selector.addStretch(1)
        camera_view.addLayout(camera_selector)
        self.live_video = QLabel("Waiting for live video")
        self.live_video.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.live_video.setMinimumSize(850, 520)
        self.live_video.setStyleSheet("background:#111; color:#aaa;")
        camera_view.addWidget(self.live_video, 1)
        layout.addLayout(camera_view, 3)

        side = QVBoxLayout()
        video_box = QGroupBox("Video / Pose Transport")
        video_form = QFormLayout(video_box)
        self.live_video_state = QLabel("Idle")
        self.live_video_latency = QLabel("--")
        self.live_pose_latency = QLabel("--")
        self.live_video_fps = QLabel("--")
        self.live_bitrate = QLabel("--")
        self.live_clock_offset = QLabel("--")
        for label, value in [
            ("State", self.live_video_state),
            ("Video latency", self.live_video_latency),
            ("Pose latency", self.live_pose_latency),
            ("Decoded FPS", self.live_video_fps),
            ("Bitrate", self.live_bitrate),
            ("Clock offset", self.live_clock_offset),
        ]:
            video_form.addRow(label, value)
        side.addWidget(video_box)

        aruco_box = QGroupBox("ArUco 夹爪逐帧测距")
        aruco_form = QFormLayout(aruco_box)
        self.live_aruco_state = QLabel("Disabled / waiting")
        self.live_aruco_ids = QLabel("--")
        self.live_aruco_depth = QLabel("--")
        self.live_aruco_raw_distance = QLabel("--")
        self.live_aruco_calibrated_distance = QLabel("--")
        self.live_aruco_filtered_distance = QLabel("--")
        for label, value in [
            ("状态", self.live_aruco_state),
            ("检测 ID", self.live_aruco_ids),
            ("标记深度", self.live_aruco_depth),
            ("相机 X 轴原始宽度", self.live_aruco_raw_distance),
            ("校准后夹爪开口", self.live_aruco_calibrated_distance),
            ("滤波后夹爪开口", self.live_aruco_filtered_distance),
        ]:
            value.setWordWrap(True)
            aruco_form.addRow(label, value)
        side.addWidget(aruco_box)

        sensor_box = QGroupBox("Combined Sensor")
        sensor_layout = QVBoxLayout(sensor_box)
        self.live_sensor_status = QLabel("Idle")
        self.live_sensor_latency = QLabel("--")
        sensor_layout.addWidget(self.live_sensor_status)
        sensor_layout.addWidget(self.live_sensor_latency)
        self.live_sensor_table = self._make_sensor_table()
        sensor_layout.addWidget(self.live_sensor_table)
        side.addWidget(sensor_box, 1)
        layout.addLayout(side, 1)
        return tab

    def _build_replay_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        splitter = QSplitter()
        layout.addWidget(splitter)

        library = QWidget()
        library_layout = QVBoxLayout(library)
        refresh = QPushButton("Refresh Experiments")
        refresh.clicked.connect(self.refresh_experiments)
        library_layout.addWidget(refresh)
        self.experiment_list = QListWidget()
        self.experiment_list.currentRowChanged.connect(self.load_experiment)
        library_layout.addWidget(self.experiment_list)

        uploads_box = QGroupBox("Phone Upload Files")
        uploads_layout = QVBoxLayout(uploads_box)
        uploads_layout.setContentsMargins(8, 8, 8, 8)
        uploads_layout.setSpacing(5)
        self.phone_upload_status = QLabel("Select an experiment")
        self.phone_upload_status.setWordWrap(True)
        uploads_layout.addWidget(self.phone_upload_status)
        self.zarr_export_status = QLabel("Zarr: waiting for a complete experiment")
        self.zarr_export_status.setWordWrap(True)
        uploads_layout.addWidget(self.zarr_export_status)
        self.phone_upload_table = QTableWidget(0, 3)
        self.phone_upload_table.setHorizontalHeaderLabels(["Type", "File", "Size"])
        self.phone_upload_table.verticalHeader().setVisible(False)
        self.phone_upload_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.phone_upload_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.phone_upload_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.phone_upload_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.phone_upload_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.phone_upload_table.setMinimumHeight(175)
        uploads_layout.addWidget(self.phone_upload_table)
        self.open_experiment_folder_button = QPushButton("Open Selected Folder")
        self.open_experiment_folder_button.setEnabled(False)
        self.open_experiment_folder_button.clicked.connect(self.open_selected_experiment_folder)
        uploads_layout.addWidget(self.open_experiment_folder_button)
        self.offline_gripper_status = QLabel("Offline gripper: select an experiment")
        self.offline_gripper_status.setWordWrap(True)
        uploads_layout.addWidget(self.offline_gripper_status)
        self.process_gripper_button = QPushButton("Process / Reprocess 0.5× Gripper Distance")
        self.process_gripper_button.setEnabled(False)
        self.process_gripper_button.clicked.connect(self.process_selected_gripper_video)
        uploads_layout.addWidget(self.process_gripper_button)
        library_layout.addWidget(uploads_box)
        splitter.addWidget(library)

        replay = QWidget()
        replay_layout = QVBoxLayout(replay)
        top = QHBoxLayout()
        replay_camera_view = QVBoxLayout()
        replay_camera_selector = QHBoxLayout()
        replay_camera_selector.addWidget(QLabel("Recorded camera"))
        self.replay_camera_combo = QComboBox()
        self.replay_camera_combo.addItem("1× Main (ARKit)", "main")
        self.replay_camera_combo.addItem("0.5× Ultra-wide (ArUco)", "ultrawide")
        self.replay_camera_combo.currentIndexChanged.connect(self.change_replay_camera)
        replay_camera_selector.addWidget(self.replay_camera_combo)
        replay_camera_selector.addStretch(1)
        replay_camera_view.addLayout(replay_camera_selector)
        self.replay_video = QLabel("Select an experiment")
        self.replay_video.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.replay_video.setMinimumSize(720, 400)
        self.replay_video.setStyleSheet("background:#111; color:#aaa;")
        replay_camera_view.addWidget(self.replay_video, 1)
        top.addLayout(replay_camera_view, 3)

        values_widget = QWidget()
        values = QVBoxLayout(values_widget)
        values.setContentsMargins(0, 0, 0, 0)
        values.setSpacing(5)
        pose_box = QGroupBox("Pose at Cursor")
        pose_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        pose_form = QFormLayout(pose_box)
        pose_form.setVerticalSpacing(4)
        self.pose_values = {name: QLabel("--") for name in ("sequence", "position", "quaternion")}
        pose_form.addRow("Sequence", self.pose_values["sequence"])
        pose_form.addRow("Position", self.pose_values["position"])
        pose_form.addRow("Quaternion", self.pose_values["quaternion"])
        values.addWidget(pose_box)

        transport_box = QGroupBox("Propagation at Cursor")
        transport_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        transport_form = QFormLayout(transport_box)
        transport_form.setVerticalSpacing(3)
        self.transport_values = {
            name: QLabel("--")
            for name in ("video_latency", "pose_latency", "raw_latency", "clock_offset", "fps", "bitrate", "drops")
        }
        for label, key in [
            ("Video corrected", "video_latency"),
            ("Pose corrected", "pose_latency"),
            ("Raw clock delta", "raw_latency"),
            ("Clock offset", "clock_offset"),
            ("FPS", "fps"),
            ("Bitrate", "bitrate"),
            ("Drops", "drops"),
        ]:
            transport_form.addRow(label, self.transport_values[key])
        values.addWidget(transport_box)

        gripper_box = QGroupBox("Offline Gripper at Cursor")
        gripper_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        gripper_form = QFormLayout(gripper_box)
        gripper_form.setVerticalSpacing(3)
        self.gripper_values = {
            name: QLabel("--")
            for name in ("status", "raw", "calibrated", "smoothed")
        }
        gripper_form.addRow("Status", self.gripper_values["status"])
        gripper_form.addRow("Raw X width", self.gripper_values["raw"])
        gripper_form.addRow("Calibrated", self.gripper_values["calibrated"])
        gripper_form.addRow("Offline stable", self.gripper_values["smoothed"])
        values.addWidget(gripper_box)
        values.addWidget(QLabel("Magnetic sensor values"))
        self.replay_sensor_table = self._make_sensor_table()
        values.addWidget(self.replay_sensor_table)
        values.addStretch(1)
        values_scroll = QScrollArea()
        values_scroll.setWidgetResizable(True)
        values_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        values_scroll.setWidget(values_widget)
        top.addWidget(values_scroll, 2)
        replay_layout.addLayout(top, 3)

        controls = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self.toggle_playback)
        controls.addWidget(self.play_button)
        self.timeline = QSlider(Qt.Orientation.Horizontal)
        self.timeline.setRange(0, 1)
        self.timeline.valueChanged.connect(self.seek_from_slider)
        controls.addWidget(self.timeline, 1)
        self.time_label = QLabel("0.000 / 0.000 s")
        controls.addWidget(self.time_label)
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["0.25x", "0.5x", "1.0x", "2.0x"])
        self.speed_combo.setCurrentText("1.0x")
        controls.addWidget(self.speed_combo)
        replay_layout.addLayout(controls)

        options = QHBoxLayout()
        self.show_corrected = QCheckBox("Corrected latency")
        self.show_corrected.setChecked(True)
        self.show_raw = QCheckBox("Raw clock delta")
        self.show_bitrate = QCheckBox("Bitrate")
        self.show_bitrate.setChecked(True)
        self.show_fps = QCheckBox("FPS")
        self.show_fps.setChecked(True)
        for checkbox in (self.show_corrected, self.show_raw, self.show_bitrate, self.show_fps):
            checkbox.toggled.connect(self.rebuild_transport_plot)
            options.addWidget(checkbox)
        options.addStretch(1)
        replay_layout.addLayout(options)

        self.plot_tabs = QTabWidget()
        self.pose_plot = self._make_plot("Position", "m")
        self.magnetic_plot = self._make_plot("Magnetic magnitude change", "Δ|B|")
        self.transport_plot = self._make_plot("Transport", "")
        self.gripper_plot = self._make_plot("Offline gripper distance", "mm")
        self.plot_tabs.addTab(self.pose_plot, "Pose")
        self.plot_tabs.addTab(self.magnetic_plot, "Sensor")
        self.plot_tabs.addTab(self.transport_plot, "Propagation")
        self.plot_tabs.addTab(self.gripper_plot, "Gripper")
        self.plot_tabs.setMinimumHeight(270)
        self.gripper_plot.setMinimumHeight(235)
        replay_layout.addWidget(self.plot_tabs, 3)
        splitter.addWidget(replay)
        splitter.setSizes([360, 1140])
        return tab

    @staticmethod
    def _make_sensor_table() -> QTableWidget:
        table = QTableWidget(5, 6)
        table.setHorizontalHeaderLabels(["Chip", "T", "X", "Y", "Z", "|B|"])
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(27)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setMinimumSectionSize(45)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setAlternatingRowColors(True)
        table.setFixedHeight(170)
        for row in range(5):
            table.setItem(row, 0, QTableWidgetItem(f"S{row}"))
        return table

    @staticmethod
    def _make_plot(title: str, units: str):
        if pg is None:
            detail = f": {PYQTGRAPH_IMPORT_ERROR}" if PYQTGRAPH_IMPORT_ERROR else ""
            label = QLabel(f"Plot module failed to load{detail}")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            return label
        plot = pg.PlotWidget(title=title)
        plot.showGrid(x=True, y=True, alpha=0.2)
        plot.setLabel("bottom", "Experiment time", units="s")
        if units:
            plot.setLabel("left", units=units)
        plot.addLegend()
        return plot

    def toggle_services(self) -> None:
        if self.video_worker is None:
            self.start_services()
        else:
            self.stop_services()

    def start_services(self) -> None:
        if self.video_worker is not None:
            return
        try:
            bind = self.bind_edit.text().strip() or "0.0.0.0"
            video_port = int(self.video_port_edit.text())
            aruco_video_port = int(self.aruco_video_port_edit.text())
            pose_port = int(self.pose_port_edit.text())
            combined_port = int(self.combined_port_edit.text())
            upload_port = int(self.upload_port_edit.text())
        except ValueError:
            self.set_service_status("Ports must be integers")
            return
        root = Path(self.root_edit.text()).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.diagnostics.set_root(root)
        upload_ready = self.upload_bridge.start(bind, upload_port, root)
        self.refresh_experiments()

        try:
            aruco_config = self.aruco_panel.current_config()
        except Exception as exc:
            aruco_config = None
            self.aruco_panel.config_status.setText(f"配置无效，ArUco 未启动：{exc}")
            self.aruco_panel.config_status.setStyleSheet("color:#c62828;")

        self.video_worker = VideoReceiverThread(
            bind,
            video_port,
            pose_port,
            aruco_config=None,
        )
        self.video_worker.frame_ready.connect(self.update_live_video)
        self.video_worker.video_metrics.connect(self.update_live_video_metrics)
        self.video_worker.pose_metrics.connect(self.update_live_pose_metrics)
        self.video_worker.start()

        self.aruco_video_worker = VideoReceiverThread(
            bind,
            aruco_video_port,
            None,
            aruco_config=aruco_config,
        )
        self.aruco_video_worker.frame_ready.connect(self.update_live_ultrawide_video)
        self.aruco_video_worker.video_metrics.connect(self.update_live_ultrawide_video_metrics)
        self.aruco_video_worker.aruco_metrics.connect(self.update_live_aruco_metrics)
        self.aruco_video_worker.start()

        self.combined_worker = CombinedReceiverThread(
            bind,
            combined_port,
            self.phone_ip_edit.text().strip(),
            5559,
            video_port,
        )
        self.combined_worker.metrics_ready.connect(self.update_live_combined_metrics)
        self.combined_worker.status_changed.connect(self.live_sensor_status.setText)
        self.combined_worker.start()
        self.services_button.setText("Stop Monitor")
        if upload_ready:
            self.upload_health.setText(f"Upload storage: READY · listening on {bind}:{upload_port}")
            self.upload_health.setStyleSheet("color:#1b5e20; font-weight:800;")
            self.set_service_status(
                "1x video plus 0.5x ArUco video, pose, sensor, upload, and diagnostics started"
            )
        else:
            self.upload_health.setText(
                "UPLOAD STORAGE OFFLINE · "
                f"{self.upload_bridge.last_error or 'unknown startup error'}"
            )
            self.upload_health.setStyleSheet("color:#c62828; font-weight:800;")
            self.set_service_status(
                "UPLOAD NOT RUNNING: "
                f"{self.upload_bridge.last_error or 'unknown startup error'}. "
                "Phone recordings are not being saved to this PC."
            )
        QTimer.singleShot(3000, self._schedule_offline_gripper_backfill)

    def stop_services(self) -> None:
        if self.video_worker is not None:
            self.video_worker.stop()
            self.video_worker.wait(1500)
            self.video_worker = None
        if self.aruco_video_worker is not None:
            self.aruco_video_worker.stop()
            self.aruco_video_worker.wait(1500)
            self.aruco_video_worker = None
        if self.combined_worker is not None:
            self.combined_worker.stop()
            self.combined_worker.wait(1500)
            self.combined_worker = None
        self.upload_bridge.stop()
        self.upload_health.setText("Upload storage: stopped")
        self.upload_health.setStyleSheet("color:#616161; font-weight:700;")
        self.services_button.setText("Start Monitor")

    def apply_aruco_configuration(self, _config: object) -> None:
        was_running = self.video_worker is not None
        if was_running:
            self.stop_services()
            self.start_services()
        self.aruco_panel.config_status.setText(
            "配置已保存并应用；监控服务已重启" if was_running else "配置已保存；启动监控后生效"
        )
        self.aruco_panel.config_status.setStyleSheet("color:#2e7d32;")

    def set_service_status(self, message: str) -> None:
        self.service_status.setText(message)
        lowered = message.lower()
        is_error = any(token in lowered for token in ("failed", "error", "not running"))
        self.service_status.setStyleSheet(
            "color:#c62828; font-weight:700;" if is_error else "color:#1b5e20; font-weight:600;"
        )

    def _schedule_offline_gripper_backfill(self) -> None:
        if self.offline_backfill_started:
            return
        try:
            if not self.aruco_panel.current_config().calibration_complete:
                return
        except Exception:
            return
        self.offline_backfill_started = True
        self.offline_gripper_processor.backfill(
            Path(self.root_edit.text()),
            self.aruco_panel.resolved_config_path(),
            self.ultrawide_intrinsics_path,
            self.upload_bridge.event_received.emit,
        )

    def _schedule_offline_gripper_directory(self, directory: Path, *, force: bool = False) -> bool:
        try:
            if not self.aruco_panel.current_config().calibration_complete:
                return False
        except Exception:
            return False
        return self.offline_gripper_processor.schedule(
            directory,
            self.aruco_panel.resolved_config_path(),
            self.ultrawide_intrinsics_path,
            self.upload_bridge.event_received.emit,
            force=force,
        )

    def _reprocess_estimated_gripper_results(self) -> None:
        for dataset in discover_experiments(Path(self.root_edit.text())):
            if (
                dataset.ultrawide_video_path is not None
                and dataset.gripper_state.get("status") == "complete"
                and str(dataset.gripper_state.get("intrinsics_source", "")).startswith("estimated")
            ):
                self._schedule_offline_gripper_directory(dataset.directory, force=True)

    def update_live_video(self, image: QImage) -> None:
        self.live_video_frames["main"] = image
        if self.live_camera_combo.currentData() == "main":
            self._show_live_image(image)

    def update_live_ultrawide_video(self, image: QImage) -> None:
        self.live_video_frames["ultrawide"] = image
        if self.live_camera_combo.currentData() == "ultrawide":
            self._show_live_image(image)

    def _show_live_image(self, image: QImage) -> None:
        self.live_video.setPixmap(
            QPixmap.fromImage(image).scaled(
                self.live_video.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def change_live_camera(self, _index: int = -1) -> None:
        camera = str(self.live_camera_combo.currentData() or "main")
        image = self.live_video_frames.get(camera)
        if image is None:
            self.live_video.clear()
            label = "1×" if camera == "main" else "0.5×"
            self.live_video.setText(f"Waiting for live {label} video")
        else:
            self._show_live_image(image)
        self._show_selected_live_video_metrics()

    def update_live_video_metrics(self, metrics: dict) -> None:
        self.live_video_metrics_by_camera["main"] = metrics
        if self.live_camera_combo.currentData() == "main":
            self._show_selected_live_video_metrics()
        identifier = int(metrics.get("frame_id", 0))
        if identifier > self.last_video_id and metrics.get("capture_timestamp") is not None:
            self.last_video_id = identifier
            self.diagnostics.record(self._diagnostic_row("video", identifier, metrics))

    def update_live_ultrawide_video_metrics(self, metrics: dict) -> None:
        self.live_video_metrics_by_camera["ultrawide"] = metrics
        camera_intrinsics = metrics.get("camera_intrinsics")
        if camera_intrinsics is not None:
            try:
                values = intrinsics_to_dict(camera_intrinsics)
                if values != self.last_ultrawide_intrinsics_values:
                    save_ultrawide_intrinsics(self.ultrawide_intrinsics_path, camera_intrinsics)
                    self.last_ultrawide_intrinsics_values = values
                    self._schedule_offline_gripper_backfill()
                    self._reprocess_estimated_gripper_results()
            except (AttributeError, TypeError, ValueError, OSError):
                pass
        if self.live_camera_combo.currentData() == "ultrawide":
            self._show_selected_live_video_metrics()
        identifier = int(metrics.get("frame_id", 0))
        if identifier > self.last_ultrawide_video_id and metrics.get("capture_timestamp") is not None:
            self.last_ultrawide_video_id = identifier
            self.diagnostics.record(self._diagnostic_row("ultrawide_video", identifier, metrics))

    def _show_selected_live_video_metrics(self) -> None:
        camera = str(self.live_camera_combo.currentData() or "main")
        metrics = self.live_video_metrics_by_camera.get(camera, {})
        self.live_video_state.setText(str(metrics.get("status", "--")))
        self.live_video_latency.setText(metric_text(metrics.get("latency_ms"), " ms"))
        self.live_video_fps.setText(metric_text(metrics.get("fps")))
        self.live_bitrate.setText(metric_text(metrics.get("bitrate_mbps"), " Mbps", 2))
        self.live_clock_offset.setText(metric_text(metrics.get("clock_offset_ms"), " ms"))

    def update_live_pose_metrics(self, metrics: dict) -> None:
        self.live_pose_latency.setText(metric_text(metrics.get("latency_ms"), " ms"))
        identifier = int(metrics.get("sequence", 0))
        if identifier > self.last_pose_id and metrics.get("sender_timestamp") is not None:
            self.last_pose_id = identifier
            self.diagnostics.record(self._diagnostic_row("pose", identifier, metrics))

    def update_live_aruco_metrics(self, metrics: dict) -> None:
        self.aruco_panel.update_live_result(metrics)
        status = str(metrics.get("status", "--"))
        measurement = metrics.get("measurement") or {}
        depths = measurement.get("marker_depth_m") or {}
        depth_values = [
            f"ID {marker_id}: {float(value) * 1000.0:.1f} mm"
            for marker_id, value in depths.items()
            if isinstance(value, (int, float))
        ]
        nominal_depth = measurement.get("nominal_marker_depth_m")
        depth_tolerance = measurement.get("marker_depth_tolerance_m")
        allowed_text = ""
        if isinstance(nominal_depth, (int, float)) and isinstance(depth_tolerance, (int, float)):
            minimum_depth = (float(nominal_depth) - float(depth_tolerance)) * 1000.0
            maximum_depth = (float(nominal_depth) + float(depth_tolerance)) * 1000.0
            allowed_text = f"；允许 {minimum_depth:.1f}–{maximum_depth:.1f} mm"
        self.live_aruco_depth.setText(", ".join(depth_values) + allowed_text if depth_values else "--")
        if status == "marker_depth_out_of_range":
            self.live_aruco_state.setText("深度超限；到 ArUco Gripper 点击“使用当前深度并应用”")
        else:
            self.live_aruco_state.setText(status)
        self.live_aruco_state.setStyleSheet(
            "color:#2e7d32; font-weight:600;"
            if status == "tracking_gripper_distance"
            else "color:#ef6c00;"
        )
        ids = metrics.get("detected_ids") or []
        self.live_aruco_ids.setText(", ".join(str(value) for value in ids) if ids else "--")
        distance = metrics.get("gripper_distance") or {}
        raw_m = distance.get("raw_marker_x_distance_m")
        calibrated_mm = distance.get("calibrated_mm")
        filtered_mm = distance.get("filtered_mm")
        calibration_complete = distance.get("calibration_complete") is True
        self.live_aruco_raw_distance.setText(
            f"{float(raw_m) * 1000.0:.4f} mm" if isinstance(raw_m, (int, float)) else "--"
        )
        self.live_aruco_calibrated_distance.setText(
            f"{float(calibrated_mm):.4f} mm"
            if calibration_complete and isinstance(calibrated_mm, (int, float))
            else "未完成两点标定"
            if distance
            else "--"
        )
        self.live_aruco_filtered_distance.setText(
            f"{float(filtered_mm):.4f} mm"
            if calibration_complete and isinstance(filtered_mm, (int, float))
            else "未完成两点标定"
            if distance
            else "--"
        )

    def update_live_combined_metrics(self, metrics: dict) -> None:
        self.live_sensor_status.setText(str(metrics.get("status", "--")))
        self.live_sensor_latency.setText(
            f"Latency {metric_text(metrics.get('latency_ms'), ' ms')}  Rate {metric_text(metrics.get('fps'), ' Hz')}"
        )
        self._update_sensor_table(self.live_sensor_table, metrics.get("chips") or ())
        identifier = int(metrics.get("packet_sequence", 0))
        if identifier > self.last_combined_id:
            self.last_combined_id = identifier
            self.diagnostics.record(self._diagnostic_row("combined", identifier, metrics))

    @staticmethod
    def _diagnostic_row(kind: str, identifier: int, metrics: dict) -> dict[str, object]:
        sender_time = metrics.get("capture_timestamp", metrics.get("sender_timestamp", ""))
        receive_time = metrics.get("first_receive_wall_time", metrics.get("receive_wall_time", ""))
        return {
            "kind": kind,
            "identifier": identifier,
            "sender_time": sender_time,
            "pc_receive_time": receive_time,
            "pc_decode_time": metrics.get("decode_wall_time", ""),
            "experiment_time": "",
            "raw_latency_ms": metrics.get("raw_latency_ms", ""),
            "corrected_latency_ms": metrics.get("latency_ms", ""),
            "clock_offset_ms": metrics.get("clock_offset_ms", ""),
            "fps": metrics.get("fps", ""),
            "bitrate_mbps": metrics.get("bitrate_mbps", ""),
            "dropped_frames": metrics.get("dropped_frames", ""),
            "packets": metrics.get("packets", ""),
            "bytes": metrics.get("bytes", ""),
        }

    def on_server_event(self, event: dict) -> None:
        if event.get("type") == "experiment_control":
            self.diagnostics.handle_control(event)
            self.set_service_status(f"Experiment {event.get('event')}: {event.get('experiment_id')}")
        if event.get("type") == "upload":
            self.set_service_status(f"Received {event.get('component')} for {event.get('capture_id')}")
        if event.get("type") == "zarr":
            status = event.get("status", "--")
            self.set_service_status(f"Zarr {status}: {event.get('capture_id')}")
        if event.get("type") == "offline_gripper":
            status = str(event.get("status", "--"))
            if status == "complete":
                rate = float(event.get("detection_rate", 0.0)) * 100.0
                self.set_service_status(f"Offline gripper complete: {rate:.1f}% valid frames")
            elif status == "failed":
                self.set_service_status(f"Offline gripper failed: {event.get('error', 'unknown error')}")
            else:
                self.set_service_status(f"Offline gripper: {status}")
        if event.get("type") == "upload" and event.get("complete"):
            uploaded_path = Path(str(event.get("path", "")))
            if uploaded_path.is_file():
                self._schedule_offline_gripper_directory(uploaded_path.parent)
        self.refresh_experiments()

    def refresh_experiments(self) -> None:
        current_id = self.dataset.experiment_id if self.dataset else None
        self.datasets = discover_experiments(Path(self.root_edit.text()))
        self.experiment_list.blockSignals(True)
        self.experiment_list.clear()
        selected_row = -1
        for index, dataset in enumerate(self.datasets):
            item = QListWidgetItem(dataset.display_name)
            item.setToolTip(str(dataset.directory))
            self.experiment_list.addItem(item)
            if dataset.experiment_id == current_id:
                selected_row = index
        self.experiment_list.blockSignals(False)
        if selected_row < 0 and self.datasets:
            selected_row = 0
        if selected_row >= 0:
            self.experiment_list.setCurrentRow(selected_row)
        else:
            self.dataset = None
            self._update_phone_upload_files(None)
            self._update_offline_gripper_status(None)

    def load_experiment(self, row: int) -> None:
        if row < 0 or row >= len(self.datasets):
            return
        self.dataset = ExperimentDataset.load(self.datasets[row].directory)
        self._update_phone_upload_files(self.dataset.directory)
        self._update_offline_gripper_status(self.dataset)
        self.playback.open(self._selected_replay_video_path())
        self.playing = False
        self.play_button.setText("Play")
        self.play_time = 0.0
        self._update_sensor_table(self.replay_sensor_table, ())
        self.timeline.setRange(0, max(1, int(self.dataset.duration_seconds * 1000)))
        self._build_data_plots()
        self.update_replay_cursor()

    def process_selected_gripper_video(self) -> None:
        if self.dataset is None or self.dataset.ultrawide_video_path is None:
            return
        if self._schedule_offline_gripper_directory(self.dataset.directory, force=True):
            self.offline_gripper_status.setText("Offline gripper: queued...")
            self.process_gripper_button.setEnabled(False)
        else:
            self.offline_gripper_status.setText(
                "Offline gripper: two-point calibration must be saved before processing"
            )

    def _update_offline_gripper_status(self, dataset: ExperimentDataset | None) -> None:
        if dataset is None:
            self.offline_gripper_status.setText("Offline gripper: select an experiment")
            self.process_gripper_button.setEnabled(False)
            return
        has_video = dataset.ultrawide_video_path is not None
        state = dataset.gripper_state
        status = str(state.get("status", "not processed"))
        if status == "complete":
            rate = float(state.get("detection_rate", 0.0)) * 100.0
            source = str(state.get("intrinsics_source", "--"))
            text = f"Offline gripper: complete · {rate:.1f}% valid · intrinsics: {source}"
        elif status == "failed":
            text = f"Offline gripper: failed · {state.get('error', 'unknown error')}"
        elif not has_video:
            text = "Offline gripper: no saved 0.5× video"
        else:
            text = f"Offline gripper: {status}"
        self.offline_gripper_status.setText(text)
        self.process_gripper_button.setEnabled(has_video and status not in {"queued", "running"})

    def change_replay_camera(self, _index: int = -1) -> None:
        if self.dataset is None:
            return
        video_path = self._selected_replay_video_path()
        self.playback.open(video_path)
        self.replay_video.clear()
        if video_path is None:
            label = "1×" if self.replay_camera_combo.currentData() == "main" else "0.5×"
            self.replay_video.setText(f"No saved {label} video in this experiment")
        self.update_replay_cursor()

    def _selected_replay_video_path(self) -> Path | None:
        if self.dataset is None:
            return None
        if self.replay_camera_combo.currentData() == "ultrawide":
            return self.dataset.ultrawide_video_path
        return self.dataset.video_path

    def _selected_replay_video_offset(self) -> float:
        if self.dataset is None:
            return 0.0
        if self.replay_camera_combo.currentData() == "ultrawide":
            return self.dataset.ultrawide_video_start_offset_seconds
        return self.dataset.video_start_offset_seconds

    def open_selected_experiment_folder(self) -> None:
        if self.dataset is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.dataset.directory)))

    def _update_phone_upload_files(self, directory: Path | None) -> None:
        self.phone_upload_table.setRowCount(0)
        self.open_experiment_folder_button.setEnabled(directory is not None and directory.is_dir())
        if directory is None or not directory.is_dir():
            self.phone_upload_status.setText("Select an experiment")
            self.zarr_export_status.setText("Zarr: waiting for a complete experiment")
            return

        state = {}
        state_path = directory / "upload_state.json"
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except (OSError, json.JSONDecodeError):
            pass

        components = state.get("components") if isinstance(state.get("components"), dict) else {}
        component_by_name = {
            Path(filename).name: str(component)
            for component, filename in components.items()
            if isinstance(filename, str)
        }
        files = sorted(
            (
                path
                for path in directory.iterdir()
                if path.is_file()
                and path.name not in PC_GENERATED_EXPERIMENT_FILES
                and not path.name.endswith(".part")
            ),
            key=lambda path: (self._phone_file_order(component_by_name.get(path.name, ""), path.name), path.name),
        )

        self.phone_upload_table.setRowCount(len(files))
        for row, path in enumerate(files):
            component = component_by_name.get(path.name) or self._infer_phone_component(path.name)
            type_item = QTableWidgetItem(self._phone_component_label(component))
            name_item = QTableWidgetItem(path.name)
            name_item.setToolTip(str(path))
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            size_item = QTableWidgetItem(file_size_text(size))
            size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.phone_upload_table.setItem(row, 0, type_item)
            self.phone_upload_table.setItem(row, 1, name_item)
            self.phone_upload_table.setItem(row, 2, size_item)

        uploaded = len(state.get("uploaded_components", [])) if isinstance(state.get("uploaded_components"), list) else len(files)
        expected = state.get("expected_files")
        if expected:
            progress = f"{uploaded}/{expected} files"
        else:
            progress = f"{len(files)} files"
        if state.get("complete"):
            status = f"Complete phone upload · {progress}"
        elif state:
            status = f"Receiving from phone · {progress}"
        else:
            status = f"Imported/legacy experiment · {progress}"
        self.phone_upload_status.setText(status)

        zarr_state = {}
        try:
            loaded = json.loads((directory / "zarr_state.json").read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                zarr_state = loaded
        except (OSError, json.JSONDecodeError):
            pass
        zarr_status = str(zarr_state.get("status", ""))
        if (directory / "dataset.zarr").is_dir() and zarr_status == "complete":
            frames = zarr_state.get("frames")
            suffix = f" · {frames} frames" if frames is not None else ""
            self.zarr_export_status.setText(f"Zarr: ready · dataset.zarr{suffix}")
        elif zarr_status == "running":
            self.zarr_export_status.setText("Zarr: converting video and synchronized data...")
        elif zarr_status == "queued":
            self.zarr_export_status.setText("Zarr: queued for automatic conversion")
        elif zarr_status == "failed":
            self.zarr_export_status.setText(f"Zarr: failed · {zarr_state.get('error', 'unknown error')}")
        else:
            self.zarr_export_status.setText("Zarr: waiting for upload completion")

    @staticmethod
    def _infer_phone_component(filename: str) -> str:
        lowered = filename.lower()
        if lowered.startswith("pose"):
            return "pose_csv"
        if lowered.startswith("magnetic"):
            return "magnetic_csv"
        if lowered.startswith("sender_transport"):
            return "sender_transport"
        if lowered.startswith("ultrawide_video"):
            return "ultrawide_video"
        if lowered.startswith("aruco_gripper"):
            return "aruco_gripper"
        if lowered.startswith("video"):
            return "video"
        if "manifest" in lowered:
            return "manifest"
        return "file"

    @staticmethod
    def _phone_component_label(component: str) -> str:
        return {
            "pose_csv": "Pose",
            "magnetic_csv": "Magnetic",
            "sender_transport": "Sender stats",
            "video": "Video",
            "ultrawide_video": "0.5× video",
            "aruco_gripper": "Offline gripper",
            "manifest": "Manifest",
        }.get(component, "File")

    @staticmethod
    def _phone_file_order(component: str, filename: str) -> int:
        inferred = component or ExperimentMonitorWindow._infer_phone_component(filename)
        return {
            "video": 0,
            "ultrawide_video": 1,
            "aruco_gripper": 2,
            "pose_csv": 3,
            "magnetic_csv": 4,
            "sender_transport": 5,
            "manifest": 6,
        }.get(inferred, 7)

    def toggle_playback(self) -> None:
        if self.dataset is None:
            return
        if self.play_time >= self.dataset.duration_seconds:
            self.play_time = 0.0
        self.playing = not self.playing
        self.last_tick = time.monotonic()
        self.play_button.setText("Pause" if self.playing else "Play")

    def seek_from_slider(self, value: int) -> None:
        if self.dataset is None:
            return
        self.play_time = value / 1000.0
        self.update_replay_cursor()

    def tick(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_tick
        self.last_tick = now
        if not self.playing or self.dataset is None:
            return
        speed = float(self.speed_combo.currentText().removesuffix("x"))
        self.play_time = min(self.dataset.duration_seconds, self.play_time + elapsed * speed)
        self.timeline.blockSignals(True)
        self.timeline.setValue(int(self.play_time * 1000))
        self.timeline.blockSignals(False)
        self.update_replay_cursor()
        if self.play_time >= self.dataset.duration_seconds:
            self.playing = False
            self.play_button.setText("Play")

    def update_replay_cursor(self) -> None:
        dataset = self.dataset
        if dataset is None:
            return
        self.time_label.setText(f"{self.play_time:.3f} / {dataset.duration_seconds:.3f} s")
        image = self.playback.frame_at(self.play_time - self._selected_replay_video_offset())
        if image is not None:
            self.replay_video.setPixmap(
                QPixmap.fromImage(image).scaled(
                    self.replay_video.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        elif self._selected_replay_video_path() is None:
            self.replay_video.clear()
            label = "1×" if self.replay_camera_combo.currentData() == "main" else "0.5×"
            self.replay_video.setText(f"No saved {label} video in this experiment")

        pose = dataset.pose.nearest(self.play_time)
        if pose:
            self.pose_values["sequence"].setText(str(pose.get("sequence", "--")))
            self.pose_values["position"].setText(
                f"({pose.get('x', '--')}, {pose.get('y', '--')}, {pose.get('z', '--')})"
            )
            self.pose_values["quaternion"].setText(
                f"({pose.get('qx', '--')}, {pose.get('qy', '--')}, {pose.get('qz', '--')}, {pose.get('qw', '--')})"
            )

        magnetic = dataset.magnetic.nearest(self.play_time)
        if magnetic:
            chips = [
                tuple(_safe_float(magnetic.get(f"s{index}_{axis}")) for axis in ("t", "x", "y", "z"))
                for index in range(5)
            ]
            self._update_sensor_table(self.replay_sensor_table, chips)
        else:
            self._update_sensor_table(self.replay_sensor_table, ())

        video_transport = self._nearest_kind(dataset.receiver_transport, self.play_time, "video")
        pose_transport = self._nearest_kind(dataset.receiver_transport, self.play_time, "pose")
        sender_transport = dataset.sender_transport.nearest(self.play_time)
        self.transport_values["video_latency"].setText(
            metric_text((video_transport or {}).get("corrected_latency_ms"), " ms")
        )
        self.transport_values["pose_latency"].setText(
            metric_text((pose_transport or {}).get("corrected_latency_ms"), " ms")
        )
        selected_transport = video_transport or pose_transport or {}
        self.transport_values["raw_latency"].setText(metric_text(selected_transport.get("raw_latency_ms"), " ms"))
        self.transport_values["clock_offset"].setText(metric_text(selected_transport.get("clock_offset_ms"), " ms"))
        self.transport_values["fps"].setText(
            metric_text(selected_transport.get("fps", (sender_transport or {}).get("sent_fps")))
        )
        self.transport_values["bitrate"].setText(
            metric_text(selected_transport.get("bitrate_mbps", (sender_transport or {}).get("bitrate_mbps")), " Mbps", 2)
        )
        self.transport_values["drops"].setText(
            str(selected_transport.get("dropped_frames", (sender_transport or {}).get("dropped_frames", "--")))
        )

        gripper = dataset.gripper.nearest(self.play_time)
        if gripper:
            status = str(gripper.get("status", "--"))
            if str(gripper.get("interpolated", "0")) == "1":
                status += " (short-gap interpolation)"
            self.gripper_values["status"].setText(status)
            raw_m = _optional_float(gripper.get("raw_marker_x_distance_m"))
            self.gripper_values["raw"].setText(
                f"{raw_m * 1000.0:.4f} mm" if raw_m is not None else "--"
            )
            self.gripper_values["calibrated"].setText(
                metric_text(gripper.get("calibrated_mm"), " mm", 4)
            )
            self.gripper_values["smoothed"].setText(
                metric_text(gripper.get("offline_smoothed_mm"), " mm", 4)
            )
        else:
            for value in self.gripper_values.values():
                value.setText("--")
        for cursor in self.plot_cursors:
            cursor.setValue(self.play_time)

    @staticmethod
    def _nearest_kind(table: TimedRows, target: float, kind: str) -> dict[str, str] | None:
        candidates = [
            (abs(sample_time - target), row)
            for sample_time, row in zip(table.times, table.rows)
            if row.get("kind") == kind
        ]
        return min(candidates, key=lambda item: item[0])[1] if candidates else None

    @staticmethod
    def _update_sensor_table(table: QTableWidget, chips) -> None:
        for row in range(5):
            values = chips[row] if row < len(chips) else (math.nan,) * 4
            magnitude = math.sqrt(sum(float(value) ** 2 for value in values[1:4])) if all(
                math.isfinite(float(value)) for value in values[1:4]
            ) else math.nan
            for column, value in enumerate((*values, magnitude), start=1):
                table.setItem(row, column, QTableWidgetItem(metric_text(value, precision=4)))

    def _build_data_plots(self) -> None:
        if pg is None or self.dataset is None:
            return
        dataset = self.dataset
        self.plot_cursors = []
        for plot in (self.pose_plot, self.magnetic_plot, self.transport_plot, self.gripper_plot):
            plot.clear()
            plot.addLegend()
        colors = ["#ff5c5c", "#56d364", "#58a6ff", "#d2a8ff", "#f2cc60"]
        for index, axis in enumerate(("x", "y", "z")):
            self.pose_plot.plot(
                dataset.pose.times,
                [_safe_float(row.get(axis)) for row in dataset.pose.rows],
                pen=pg.mkPen(colors[index], width=2),
                name=axis.upper(),
            )
        for chip in range(5):
            self.magnetic_plot.plot(
                dataset.magnetic.times,
                _relative_magnetic_magnitudes(dataset.magnetic.rows, chip),
                pen=pg.mkPen(colors[chip], width=1.5),
                name=f"S{chip}",
            )
        for plot in (self.pose_plot, self.magnetic_plot):
            cursor = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#ffffff", width=1))
            plot.addItem(cursor)
            self.plot_cursors.append(cursor)
        self.rebuild_transport_plot()
        calibrated = [
            value if (value := _optional_float(row.get("calibrated_mm"))) is not None else math.nan
            for row in dataset.gripper.rows
        ]
        stable = [
            value if (value := _optional_float(row.get("offline_smoothed_mm"))) is not None else math.nan
            for row in dataset.gripper.rows
        ]
        self.gripper_plot.plot(
            dataset.gripper.times,
            calibrated,
            pen=pg.mkPen("#8c8c8c", width=1),
            name="per-frame calibrated",
        )
        self.gripper_plot.plot(
            dataset.gripper.times,
            stable,
            pen=pg.mkPen("#56d364", width=2.5),
            name="offline stable",
        )
        gripper_cursor = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#ffffff", width=1))
        self.gripper_plot.addItem(gripper_cursor)
        self.plot_cursors.append(gripper_cursor)

    def rebuild_transport_plot(self) -> None:
        if pg is None or self.dataset is None:
            return
        plot = self.transport_plot
        plot.clear()
        plot.addLegend()
        rows = self.dataset.receiver_transport.rows
        times = self.dataset.receiver_transport.times
        if self.show_corrected.isChecked():
            for kind, color in (("video", "#58a6ff"), ("pose", "#f2cc60"), ("combined", "#56d364")):
                x = [sample_time for sample_time, row in zip(times, rows) if row.get("kind") == kind]
                y = [_safe_float(row.get("corrected_latency_ms")) for row in rows if row.get("kind") == kind]
                plot.plot(x, y, pen=pg.mkPen(color, width=2), name=f"{kind} latency ms")
        if self.show_raw.isChecked():
            x = [sample_time for sample_time, row in zip(times, rows) if row.get("kind") == "video"]
            y = [_safe_float(row.get("raw_latency_ms")) for row in rows if row.get("kind") == "video"]
            plot.plot(x, y, pen=pg.mkPen("#ff7b72", width=1), name="video raw ms")
        if self.show_bitrate.isChecked():
            plot.plot(
                self.dataset.sender_transport.times,
                [_safe_float(row.get("bitrate_mbps")) for row in self.dataset.sender_transport.rows],
                pen=pg.mkPen("#d2a8ff", width=1.5),
                name="sender Mbps",
            )
        if self.show_fps.isChecked():
            plot.plot(
                self.dataset.sender_transport.times,
                [_safe_float(row.get("sent_fps")) for row in self.dataset.sender_transport.rows],
                pen=pg.mkPen("#ffa657", width=1.5),
                name="sender FPS",
            )
        cursor = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#ffffff", width=1))
        plot.addItem(cursor)
        if len(self.plot_cursors) >= 3:
            self.plot_cursors[2] = cursor
        else:
            self.plot_cursors.append(cursor)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.playback.close()
        self.stop_services()
        self.diagnostics.close()
        super().closeEvent(event)


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _optional_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _relative_magnetic_magnitudes(rows: list[dict], chip: int) -> list[float]:
    """Return each chip's magnitude relative to its first complete sample."""
    magnitudes: list[float] = []
    baseline: float | None = None
    for row in rows:
        components = (
            _optional_float(row.get(f"s{chip}_x")),
            _optional_float(row.get(f"s{chip}_y")),
            _optional_float(row.get(f"s{chip}_z")),
        )
        if any(value is None for value in components):
            magnitudes.append(math.nan)
            continue

        x, y, z = components
        magnitude = math.sqrt(x * x + y * y + z * z)
        if baseline is None:
            baseline = magnitude
        magnitudes.append(magnitude - baseline)
    return magnitudes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor and replay synchronized ARPose experiments.")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--video-port", type=int, default=5560)
    parser.add_argument("--aruco-video-port", type=int, default=5561)
    parser.add_argument("--pose-port", type=int, default=5555)
    parser.add_argument("--combined-port", type=int, default=5558)
    parser.add_argument("--upload-port", type=int, default=8000)
    parser.add_argument("--phone-ip", default="172.20.10.1")
    parser.add_argument("--experiments", default=str(get_default_upload_dir()))
    parser.add_argument(
        "--aruco-config",
        default=str(get_app_base_dir() / "config" / "umi_gripper_aruco.json"),
    )
    return parser.parse_args()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ARPose Experiment Monitor & Replay")
    acquired, mutex = acquire_single_instance_mutex()
    if not acquired:
        QMessageBox.warning(
            None,
            "监控程序已经在运行",
            "检测到另一个 ARPose Experiment Monitor。请使用已有窗口，或先完全关闭它再重试。",
        )
        return 2
    try:
        window = ExperimentMonitorWindow(parse_args())
        window.show()
        return app.exec()
    finally:
        release_single_instance_mutex(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
