from __future__ import annotations

import time
from collections import deque
import shutil
import sys
from pathlib import Path

import numpy as np
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from aruco_gripper_tracker import (
    ArucoEstimator,
    CameraIntrinsics,
    CyclicCalibrationSummary,
    TrackerConfig,
    calculate_distance_calibration,
    summarize_cyclic_calibration,
)


class ArucoConfigWidget(QWidget):
    """Compact two-point calibration UI for per-frame gripper distance."""

    apply_requested = pyqtSignal(object)

    def __init__(self, config_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project_root = (
            Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent
        )
        self.last_raw_distance_m: float | None = None
        self.last_marker_depths_m: dict[str, float] = {}
        self.recent_raw_samples: deque[tuple[float, float]] = deque(maxlen=30)
        self.distance_scale = 1.0
        self.distance_offset_m = 0.0
        self.calibration_collecting = False
        self.calibration_samples_m: list[float] = []
        self.calibration_collection_summary: CyclicCalibrationSummary | None = None
        self._ensure_initial_config(config_path)
        self._build_ui(config_path)
        self.load_configuration(show_dialog=False)

    @staticmethod
    def _ensure_initial_config(config_path: Path) -> None:
        if config_path.is_file() or not getattr(sys, "frozen", False):
            return
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        bundled = bundle_root / "config" / "umi_gripper_aruco.json"
        if bundled.is_file():
            config_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundled, config_path)

    @staticmethod
    def _double_spin(
        minimum: float,
        maximum: float,
        decimals: int,
        step: float,
        suffix: str = "",
    ) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setSingleStep(step)
        if suffix:
            spin.setSuffix(suffix)
        return spin

    def _raw_point_spin(self) -> QDoubleSpinBox:
        spin = self._double_spin(-1.0, 5000.0, 4, 0.1, " mm")
        spin.setSpecialValueText("未记录")
        spin.setValue(-1.0)
        spin.setToolTip("连续开合采集后自动填写，也可以手工修改原始 X 轴宽度")
        return spin

    def _build_ui(self, config_path: Path) -> None:
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)

        summary = QLabel(
            "用途：按 UMI-FT 方法逐帧测量夹爪开口。ID 0、ID 1 分别贴在两个活动夹爪上，"
            "原始宽度取两个标记相机 X 坐标之差。快捷流程：使用当前深度 → 合拢记录最小点 → "
            "张开记录最大点 → 保存并应用。"
        )
        summary.setWordWrap(True)
        summary.setStyleSheet(
            "padding:10px; background:#263238; color:#ffffff; border-radius:5px; font-size:14px;"
        )
        layout.addWidget(summary)

        live_box = QGroupBox("当前帧")
        live = QGridLayout(live_box)
        self.live_status_label = QLabel("等待启动监控")
        self.live_status_label.setWordWrap(True)
        live.addWidget(self.live_status_label, 0, 0, 1, 3)
        self.current_raw_label = QLabel("--")
        self.current_gap_label = QLabel("--")
        self.filtered_gap_label = QLabel("--")
        for column, (title, value) in enumerate(
            [
                ("相机 X 轴原始宽度", self.current_raw_label),
                ("校准后开口", self.current_gap_label),
                ("滤波后开口", self.filtered_gap_label),
            ]
        ):
            title_label = QLabel(title)
            title_label.setStyleSheet("color:#607d8b;")
            value.setStyleSheet("font-size:20px; font-weight:600;")
            live.addWidget(title_label, 1, column)
            live.addWidget(value, 2, column)
        self.live_detail_label = QLabel("检测 ID：--")
        live.addWidget(self.live_detail_label, 3, 0, 1, 3)
        layout.addWidget(live_box)

        marker_box = QGroupBox("1. 标记设置")
        marker_layout = QGridLayout(marker_box)
        self.dictionary_combo = QComboBox()
        self.dictionary_combo.setEditable(True)
        self.dictionary_combo.addItems(["DICT_4X4_50", "DICT_5X5_50", "DICT_6X6_50"])
        self.marker_size_mm = self._double_spin(0.1, 500.0, 3, 0.1, " mm")
        self.marker_ids_edit = QLineEdit("0,1")
        self.nominal_depth_mm_spin = self._double_spin(1.0, 1000.0, 3, 0.5, " mm")
        self.nominal_depth_mm_spin.setValue(72.0)
        self.depth_tolerance_mm_spin = self._double_spin(0.1, 200.0, 3, 0.5, " mm")
        self.depth_tolerance_mm_spin.setValue(8.0)
        for column, (label, widget, tip) in enumerate(
            [
                ("字典", self.dictionary_combo, "随附 PDF 使用 DICT_4X4_50"),
                ("黑色外边长", self.marker_size_mm, "打印后实测应为 16.000 mm"),
                ("两个标记 ID", self.marker_ids_edit, "默认 0,1；必须同时检测到两个标记"),
            ]
        ):
            marker_layout.addWidget(QLabel(label), 0, column)
            marker_layout.addWidget(widget, 1, column)
            widget.setToolTip(tip)
        marker_layout.addWidget(QLabel("标记标称深度"), 2, 0)
        marker_layout.addWidget(self.nominal_depth_mm_spin, 3, 0)
        marker_layout.addWidget(QLabel("允许深度偏差"), 2, 1)
        marker_layout.addWidget(self.depth_tolerance_mm_spin, 3, 1)
        depth_hint = QLabel("UMI-FT 默认 72 ± 8 mm；若安装结构不同，请按实际相机到标记深度修改")
        depth_hint.setWordWrap(True)
        depth_hint.setStyleSheet("color:#546e7a;")
        marker_layout.addWidget(depth_hint, 2, 2)
        self.use_current_depth_button = QPushButton("使用当前深度并应用")
        self.use_current_depth_button.setToolTip(
            "用当前两个标记深度的中点作为标称值，并自动留出至少 ±20 mm 余量"
        )
        self.use_current_depth_button.clicked.connect(self.use_current_marker_depth)
        marker_layout.addWidget(self.use_current_depth_button, 3, 2)
        layout.addWidget(marker_box)

        calibration_box = QGroupBox("2. 两点快捷标定")
        calibration = QGridLayout(calibration_box)
        calibration.addWidget(QLabel("位置"), 0, 0)
        calibration.addWidget(QLabel("实际夹爪开口（卡尺测量）"), 0, 1)
        calibration.addWidget(QLabel("记录的 X 轴宽度"), 0, 2)
        calibration.addWidget(QLabel("保持不动约 1 秒后点击"), 0, 3)

        self.minimum_gap_mm_spin = self._double_spin(0.0, 2000.0, 3, 0.1, " mm")
        self.maximum_gap_mm_spin = self._double_spin(0.0, 2000.0, 3, 0.1, " mm")
        self.minimum_raw_mm_spin = self._raw_point_spin()
        self.maximum_raw_mm_spin = self._raw_point_spin()
        self.calibration_min_cycles_spin = QSpinBox()
        self.calibration_min_cycles_spin.setRange(2, 20)
        self.calibration_min_cycles_spin.setValue(5)

        calibration.addWidget(QLabel("最小开口"), 1, 0)
        calibration.addWidget(self.minimum_gap_mm_spin, 1, 1)
        calibration.addWidget(self.minimum_raw_mm_spin, 1, 2)
        self.capture_minimum_button = QPushButton("记录当前最小点")
        self.capture_minimum_button.clicked.connect(self.capture_minimum_point)
        calibration.addWidget(self.capture_minimum_button, 1, 3)
        calibration.addWidget(QLabel("最大开口"), 2, 0)
        calibration.addWidget(self.maximum_gap_mm_spin, 2, 1)
        calibration.addWidget(self.maximum_raw_mm_spin, 2, 2)
        self.capture_maximum_button = QPushButton("记录当前最大点")
        self.capture_maximum_button.clicked.connect(self.capture_maximum_point)
        calibration.addWidget(self.capture_maximum_button, 2, 3)

        self.quick_calibration_status_label = QLabel(
            "先填写卡尺测得的最小/最大实际开口，再分别保持在两个端点并点击记录。"
        )
        self.quick_calibration_status_label.setWordWrap(True)
        self.quick_calibration_status_label.setStyleSheet("color:#1565c0;")
        calibration.addWidget(self.quick_calibration_status_label, 3, 0, 1, 4)

        quick_apply_button = QPushButton("完成：保存并应用标定")
        quick_apply_button.setStyleSheet("font-weight:600; padding:6px 16px;")
        quick_apply_button.clicked.connect(self.finish_quick_calibration)
        calibration.addWidget(quick_apply_button, 4, 0, 1, 4)

        self.show_cyclic_calibration_checkbox = QCheckBox("可选：显示连续开合稳健采集")
        calibration.addWidget(self.show_cyclic_calibration_checkbox, 5, 0, 1, 4)
        self.cyclic_calibration_widget = QWidget()
        cyclic = QGridLayout(self.cyclic_calibration_widget)
        cyclic.setContentsMargins(0, 0, 0, 0)
        cyclic.addWidget(QLabel("最少完整开合周期"), 0, 0)
        cyclic.addWidget(self.calibration_min_cycles_spin, 0, 1)
        self.start_collection_button = QPushButton("开始采集")
        self.finish_collection_button = QPushButton("结束采集并计算")
        self.finish_collection_button.setEnabled(False)
        self.clear_collection_button = QPushButton("清空本次采集")
        self.start_collection_button.clicked.connect(self.start_calibration_collection)
        self.finish_collection_button.clicked.connect(self.finish_calibration_collection)
        self.clear_collection_button.clicked.connect(self.clear_calibration_collection)
        controls = QHBoxLayout()
        controls.addWidget(self.start_collection_button)
        controls.addWidget(self.finish_collection_button)
        controls.addWidget(self.clear_collection_button)
        cyclic.addLayout(controls, 1, 0, 1, 3)
        self.collection_status_label = QLabel("尚未开始采集")
        self.collection_status_label.setWordWrap(True)
        cyclic.addWidget(self.collection_status_label, 2, 0, 1, 3)

        calculate_button = QPushButton("使用上方端点重新计算")
        calculate_button.clicked.connect(self.calculate_two_point_calibration)
        cyclic.addWidget(calculate_button, 3, 0, 1, 3)
        self.cyclic_calibration_widget.setVisible(False)
        self.show_cyclic_calibration_checkbox.toggled.connect(
            self.cyclic_calibration_widget.setVisible
        )
        calibration.addWidget(self.cyclic_calibration_widget, 6, 0, 1, 4)
        self.calibration_result_label = QLabel("尚未完成两点标定")
        self.calibration_result_label.setWordWrap(True)
        self.calibration_result_label.setStyleSheet(
            "padding:7px; background:#eceff1; border-radius:4px;"
        )
        calibration.addWidget(self.calibration_result_label, 7, 0, 1, 4)
        formula = QLabel(
            "快捷记录使用最近约 1 秒稳定帧的中位数，减少单帧抖动。连续开合采集仅在需要更高稳健性时使用。"
        )
        formula.setWordWrap(True)
        formula.setStyleSheet("color:#546e7a;")
        calibration.addWidget(formula, 8, 0, 1, 4)
        layout.addWidget(calibration_box)

        output_box = QGroupBox("3. 输出与保存")
        output = QGridLayout(output_box)
        self.enabled_checkbox = QCheckBox("启用逐帧 ArUco 测距与 UDP 输出")
        self.enabled_checkbox.setChecked(True)
        self.output_host_edit = QLineEdit("127.0.0.1")
        self.output_port_spin = QSpinBox()
        self.output_port_spin.setRange(1, 65535)
        self.output_port_spin.setValue(5570)
        self.smoothing_alpha_spin = self._double_spin(0.01, 1.0, 3, 0.05)
        self.smoothing_alpha_spin.setValue(1.0)
        self.smoothing_alpha_spin.setToolTip("1.0 表示不平滑；数值越小越平稳，但延迟越大")
        output.addWidget(self.enabled_checkbox, 0, 0, 1, 3)
        for column, (label, widget) in enumerate(
            [
                ("UDP 接收 IP", self.output_host_edit),
                ("UDP 端口", self.output_port_spin),
                ("EMA 系数", self.smoothing_alpha_spin),
            ]
        ):
            output.addWidget(QLabel(label), 1, column)
            output.addWidget(widget, 2, column)
        layout.addWidget(output_box)

        self.show_advanced_checkbox = QCheckBox("显示高级设置（配置文件、APV1 内参、检测门限）")
        layout.addWidget(self.show_advanced_checkbox)
        self.advanced_box = QGroupBox("高级设置")
        advanced = QVBoxLayout(self.advanced_box)

        file_layout = QHBoxLayout()
        self.config_path_edit = QLineEdit(str(config_path))
        browse_button = QPushButton("浏览")
        browse_button.clicked.connect(self.browse_configuration)
        load_button = QPushButton("重新加载")
        load_button.clicked.connect(self.load_configuration)
        file_layout.addWidget(QLabel("配置文件"))
        file_layout.addWidget(self.config_path_edit, 1)
        file_layout.addWidget(browse_button)
        file_layout.addWidget(load_button)
        advanced.addLayout(file_layout)

        camera_box = QGroupBox("旧 APV1 回退内参（APV2 不勾选）")
        camera = QGridLayout(camera_box)
        self.manual_intrinsics_checkbox = QCheckBox("使用手工内参")
        camera.addWidget(self.manual_intrinsics_checkbox, 0, 0, 1, 6)
        self.fx_spin = self._double_spin(0.0, 20000.0, 4, 1.0)
        self.fy_spin = self._double_spin(0.0, 20000.0, 4, 1.0)
        self.cx_spin = self._double_spin(-20000.0, 20000.0, 4, 1.0)
        self.cy_spin = self._double_spin(-20000.0, 20000.0, 4, 1.0)
        self.image_width_spin = QSpinBox()
        self.image_width_spin.setRange(1, 16384)
        self.image_width_spin.setValue(1280)
        self.image_height_spin = QSpinBox()
        self.image_height_spin.setRange(1, 16384)
        self.image_height_spin.setValue(720)
        for index, (label, widget) in enumerate(
            [
                ("fx", self.fx_spin),
                ("fy", self.fy_spin),
                ("cx", self.cx_spin),
                ("cy", self.cy_spin),
                ("宽度", self.image_width_spin),
                ("高度", self.image_height_spin),
            ]
        ):
            row = 1 + index // 3
            column = (index % 3) * 2
            camera.addWidget(QLabel(label), row, column)
            camera.addWidget(widget, row, column + 1)
        self.distortion_edit = QLineEdit("0,0,0,0,0")
        camera.addWidget(QLabel("畸变系数"), 3, 0)
        camera.addWidget(self.distortion_edit, 3, 1, 1, 5)
        self.manual_intrinsics_checkbox.toggled.connect(self._set_intrinsics_enabled)
        self._set_intrinsics_enabled(False)
        advanced.addWidget(camera_box)

        quality_box = QGroupBox("检测质量门限")
        quality = QFormLayout(quality_box)
        self.max_reprojection_spin = self._double_spin(0.1, 50.0, 3, 0.1, " px")
        self.min_perimeter_spin = self._double_spin(4.0, 10000.0, 1, 5.0, " px")
        quality.addRow("最大重投影误差", self.max_reprojection_spin)
        quality.addRow("最小标记周长", self.min_perimeter_spin)
        advanced.addWidget(quality_box)
        self.advanced_box.setVisible(False)
        self.show_advanced_checkbox.toggled.connect(self.advanced_box.setVisible)
        layout.addWidget(self.advanced_box)

        buttons = QHBoxLayout()
        save_button = QPushButton("仅保存")
        save_button.clicked.connect(lambda: self.save_configuration(apply=False))
        apply_button = QPushButton("保存并应用")
        apply_button.setStyleSheet("font-weight:600; padding:6px 16px;")
        apply_button.clicked.connect(lambda: self.save_configuration(apply=True))
        buttons.addStretch(1)
        buttons.addWidget(save_button)
        buttons.addWidget(apply_button)
        layout.addLayout(buttons)
        self.config_status = QLabel("尚未加载配置")
        self.config_status.setWordWrap(True)
        layout.addWidget(self.config_status)
        layout.addStretch(1)

    def _set_intrinsics_enabled(self, enabled: bool) -> None:
        for widget in (
            self.fx_spin,
            self.fy_spin,
            self.cx_spin,
            self.cy_spin,
            self.image_width_spin,
            self.image_height_spin,
        ):
            widget.setEnabled(enabled)

    def resolved_config_path(self) -> Path:
        path = Path(self.config_path_edit.text().strip()).expanduser()
        if not path.is_absolute():
            path = self.project_root / path
        return path.resolve()

    def browse_configuration(self) -> None:
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "选择 ArUco 测距配置",
            str(self.resolved_config_path()),
            "JSON (*.json)",
        )
        if selected:
            self.config_path_edit.setText(selected)

    @staticmethod
    def _raw_spin_m(spin: QDoubleSpinBox) -> float | None:
        return None if spin.value() < 0.0 else spin.value() / 1000.0

    def current_config(self) -> TrackerConfig:
        marker_ids = tuple(
            int(value.strip())
            for value in self.marker_ids_edit.text().replace(";", ",").split(",")
            if value.strip()
        )
        if len(marker_ids) != 2:
            raise ValueError("夹爪测距必须且只能填写两个标记 ID")

        minimum_raw_m = self._raw_spin_m(self.minimum_raw_mm_spin)
        maximum_raw_m = self._raw_spin_m(self.maximum_raw_mm_spin)
        minimum_gap_m = self.minimum_gap_mm_spin.value() / 1000.0
        maximum_gap_m = self.maximum_gap_mm_spin.value() / 1000.0
        if minimum_raw_m is not None and maximum_raw_m is not None:
            self.distance_scale, self.distance_offset_m = calculate_distance_calibration(
                minimum_raw_m,
                minimum_gap_m,
                maximum_raw_m,
                maximum_gap_m,
            )
            self._refresh_calibration_result()

        fallback = None
        if self.manual_intrinsics_checkbox.isChecked():
            fallback = CameraIntrinsics(
                fx=self.fx_spin.value(),
                fy=self.fy_spin.value(),
                cx=self.cx_spin.value(),
                cy=self.cy_spin.value(),
                image_width=self.image_width_spin.value(),
                image_height=self.image_height_spin.value(),
            )
        distortion = np.asarray(
            [float(value.strip()) for value in self.distortion_edit.text().split(",") if value.strip()],
            dtype=np.float64,
        ).reshape(-1, 1)
        if distortion.size not in (4, 5, 8, 12, 14):
            raise ValueError("畸变系数必须填写 4、5、8、12 或 14 个数")

        config = TrackerConfig(
            dictionary_name=self.dictionary_combo.currentText().strip(),
            marker_size_m=self.marker_size_mm.value() / 1000.0,
            marker_ids=marker_ids,
            distortion_coefficients=distortion,
            fallback_intrinsics=fallback,
            max_reprojection_error_px=self.max_reprojection_spin.value(),
            min_marker_perimeter_px=self.min_perimeter_spin.value(),
            tracking_enabled=self.enabled_checkbox.isChecked(),
            output_host=self.output_host_edit.text().strip(),
            output_port=self.output_port_spin.value(),
            distance_scale=self.distance_scale,
            distance_offset_m=self.distance_offset_m,
            distance_smoothing_alpha=self.smoothing_alpha_spin.value(),
            distance_measurement_mode="camera_x",
            nominal_marker_depth_m=self.nominal_depth_mm_spin.value() / 1000.0,
            marker_depth_tolerance_m=self.depth_tolerance_mm_spin.value() / 1000.0,
            calibration_min_raw_m=minimum_raw_m,
            calibration_min_gap_m=minimum_gap_m,
            calibration_max_raw_m=maximum_raw_m,
            calibration_max_gap_m=maximum_gap_m,
            calibration_min_cycles=self.calibration_min_cycles_spin.value(),
        )
        if not config.output_host:
            raise ValueError("UDP 接收 IP 不能为空")
        if config.tracking_enabled:
            ArucoEstimator(config)
        return config

    def set_config(self, config: TrackerConfig) -> None:
        self.enabled_checkbox.setChecked(config.tracking_enabled)
        self.dictionary_combo.setCurrentText(config.dictionary_name)
        self.marker_size_mm.setValue(config.marker_size_m * 1000.0)
        self.marker_ids_edit.setText(",".join(str(value) for value in config.marker_ids))
        self.output_host_edit.setText(config.output_host)
        self.output_port_spin.setValue(config.output_port)
        self.smoothing_alpha_spin.setValue(config.distance_smoothing_alpha)
        self.nominal_depth_mm_spin.setValue(config.nominal_marker_depth_m * 1000.0)
        self.depth_tolerance_mm_spin.setValue(config.marker_depth_tolerance_m * 1000.0)
        self.calibration_min_cycles_spin.setValue(config.calibration_min_cycles)
        self.distance_scale = config.distance_scale
        self.distance_offset_m = config.distance_offset_m
        self.minimum_gap_mm_spin.setValue(config.calibration_min_gap_m * 1000.0)
        self.maximum_gap_mm_spin.setValue(config.calibration_max_gap_m * 1000.0)
        self.minimum_raw_mm_spin.setValue(
            -1.0 if config.calibration_min_raw_m is None else config.calibration_min_raw_m * 1000.0
        )
        self.maximum_raw_mm_spin.setValue(
            -1.0 if config.calibration_max_raw_m is None else config.calibration_max_raw_m * 1000.0
        )
        fallback = config.fallback_intrinsics
        self.manual_intrinsics_checkbox.setChecked(fallback is not None)
        if fallback is not None:
            self.fx_spin.setValue(fallback.fx)
            self.fy_spin.setValue(fallback.fy)
            self.cx_spin.setValue(fallback.cx)
            self.cy_spin.setValue(fallback.cy)
            self.image_width_spin.setValue(fallback.image_width)
            self.image_height_spin.setValue(fallback.image_height)
        self.distortion_edit.setText(
            ",".join(f"{float(value):.9g}" for value in config.distortion_coefficients.reshape(-1))
        )
        self.max_reprojection_spin.setValue(config.max_reprojection_error_px)
        self.min_perimeter_spin.setValue(config.min_marker_perimeter_px)
        self._refresh_calibration_result()

    def _refresh_calibration_result(self) -> None:
        complete = (
            self._raw_spin_m(self.minimum_raw_mm_spin) is not None
            and self._raw_spin_m(self.maximum_raw_mm_spin) is not None
        )
        prefix = "两点标定完成" if complete else "尚未完成两点标定"
        self.calibration_result_label.setText(
            f"{prefix}　scale={self.distance_scale:.8f}　offset={self.distance_offset_m * 1000.0:.4f} mm"
        )
        self.calibration_result_label.setStyleSheet(
            "padding:7px; border-radius:4px; "
            + ("background:#e8f5e9; color:#2e7d32;" if complete else "background:#fff8e1; color:#ef6c00;")
        )

    def load_configuration(self, _checked: bool = False, show_dialog: bool = True) -> None:
        try:
            path = self.resolved_config_path()
            self.set_config(TrackerConfig.load(path))
            self.config_status.setText(f"已加载：{path}")
            self.config_status.setStyleSheet("color:#2e7d32;")
        except Exception as exc:
            self.config_status.setText(f"加载失败：{exc}")
            self.config_status.setStyleSheet("color:#c62828;")
            if show_dialog:
                QMessageBox.critical(self, "ArUco 测距配置错误", str(exc))

    def save_configuration(self, apply: bool) -> None:
        try:
            config = self.current_config()
            path = self.resolved_config_path()
            config.save(path)
            self.config_status.setText(f"已保存：{path}" + ("；正在应用" if apply else ""))
            self.config_status.setStyleSheet("color:#2e7d32;")
            if apply:
                self.apply_requested.emit(config)
        except Exception as exc:
            self.config_status.setText(f"保存失败：{exc}")
            self.config_status.setStyleSheet("color:#c62828;")
            QMessageBox.critical(self, "无法保存 ArUco 测距配置", str(exc))

    def use_current_marker_depth(self, _checked: bool = False) -> None:
        depths_mm = [
            value * 1000.0
            for value in self.last_marker_depths_m.values()
            if np.isfinite(value) and value > 0.0
        ]
        if len(depths_mm) < 2:
            QMessageBox.warning(
                self,
                "没有可用深度",
                "请先让 0.5× 画面同时看到两个标记，再点击“使用当前深度并应用”。",
            )
            return
        nominal_mm = float(np.median(depths_mm))
        maximum_deviation_mm = max(abs(value - nominal_mm) for value in depths_mm)
        tolerance_mm = max(20.0, maximum_deviation_mm + 10.0)
        self.nominal_depth_mm_spin.setValue(nominal_mm)
        self.depth_tolerance_mm_spin.setValue(tolerance_mm)
        self.quick_calibration_status_label.setText(
            f"已按当前深度设置为 {nominal_mm:.1f} ± {tolerance_mm:.1f} mm；正在保存并应用。"
        )
        self.quick_calibration_status_label.setStyleSheet("color:#2e7d32; font-weight:600;")
        self.save_configuration(apply=True)

    def _stable_recent_raw_point(self) -> tuple[float, int, float] | None:
        cutoff = time.monotonic() - 1.25
        values = np.asarray(
            [value for timestamp, value in self.recent_raw_samples if timestamp >= cutoff],
            dtype=np.float64,
        )
        if values.size < 4:
            QMessageBox.warning(
                self,
                "稳定帧不足",
                "请确认状态为“正在逐帧测量”，把夹爪保持不动约 1 秒后再点击。",
            )
            return None
        point_m = float(np.median(values))
        spread_m = float(np.percentile(values, 90.0) - np.percentile(values, 10.0))
        if spread_m > 0.0015:
            QMessageBox.warning(
                self,
                "端点仍在移动",
                f"最近帧波动约 {spread_m * 1000.0:.2f} mm。请保持不动约 1 秒后重试。",
            )
            return None
        return point_m, int(values.size), spread_m

    def capture_minimum_point(self, _checked: bool = False) -> None:
        self._capture_quick_point(self.minimum_raw_mm_spin, "最小")

    def capture_maximum_point(self, _checked: bool = False) -> None:
        self._capture_quick_point(self.maximum_raw_mm_spin, "最大")

    def _capture_quick_point(self, target: QDoubleSpinBox, label: str) -> None:
        stable = self._stable_recent_raw_point()
        if stable is None:
            return
        point_m, sample_count, spread_m = stable
        target.setValue(point_m * 1000.0)
        self.quick_calibration_status_label.setText(
            f"已记录{label}点 {point_m * 1000.0:.3f} mm（{sample_count} 帧，波动 "
            f"{spread_m * 1000.0:.3f} mm）。"
        )
        self.quick_calibration_status_label.setStyleSheet("color:#2e7d32; font-weight:600;")
        if (
            self._raw_spin_m(self.minimum_raw_mm_spin) is not None
            and self._raw_spin_m(self.maximum_raw_mm_spin) is not None
            and self.maximum_gap_mm_spin.value() > self.minimum_gap_mm_spin.value()
        ):
            self.calculate_two_point_calibration(show_dialog=False)

    def finish_quick_calibration(self, _checked: bool = False) -> None:
        if self.calculate_two_point_calibration(show_dialog=True):
            self.save_configuration(apply=True)

    def start_calibration_collection(self, _checked: bool = False) -> None:
        if self.maximum_gap_mm_spin.value() <= self.minimum_gap_mm_spin.value():
            QMessageBox.warning(self, "实际开口无效", "最大实际开口必须大于最小实际开口。")
            return
        self.calibration_samples_m.clear()
        self.calibration_collection_summary = None
        self.calibration_collecting = True
        self.start_collection_button.setEnabled(False)
        self.finish_collection_button.setEnabled(True)
        self.collection_status_label.setText(
            f"正在采集：请连续完成至少 {self.calibration_min_cycles_spin.value()} 次全闭→全开→全闭"
        )
        self.collection_status_label.setStyleSheet("color:#1565c0; font-weight:600;")

    def clear_calibration_collection(self, _checked: bool = False) -> None:
        self.calibration_collecting = False
        self.calibration_samples_m.clear()
        self.calibration_collection_summary = None
        self.start_collection_button.setEnabled(True)
        self.finish_collection_button.setEnabled(False)
        self.collection_status_label.setText("本次采集已清空；已保存的标定端点未改变")
        self.collection_status_label.setStyleSheet("color:#546e7a;")

    def _update_collection_summary(self) -> None:
        if len(self.calibration_samples_m) < 10:
            self.collection_status_label.setText(
                f"正在采集：{len(self.calibration_samples_m)} 个有效帧；等待形成完整开合范围"
            )
            return
        try:
            summary = summarize_cyclic_calibration(self.calibration_samples_m)
        except ValueError as exc:
            self.calibration_collection_summary = None
            self.collection_status_label.setText(
                f"正在采集：{len(self.calibration_samples_m)} 个有效帧；{exc}"
            )
            return
        self.calibration_collection_summary = summary
        self.collection_status_label.setText(
            f"正在采集：{summary.sample_count} 帧，检测到约 {summary.cycle_count} 个完整周期；"
            f"稳健端点 {summary.minimum_raw_m * 1000.0:.3f}–"
            f"{summary.maximum_raw_m * 1000.0:.3f} mm"
        )

    def finish_calibration_collection(self, _checked: bool = False) -> None:
        self._update_collection_summary()
        summary = self.calibration_collection_summary
        required_cycles = self.calibration_min_cycles_spin.value()
        if summary is None or summary.cycle_count < required_cycles:
            measured_cycles = 0 if summary is None else summary.cycle_count
            message = (
                f"目前只检测到约 {measured_cycles} 个完整周期，需要至少 {required_cycles} 个。"
                "请继续完整开合后再次点击结束。"
            )
            QMessageBox.warning(self, "标定周期不足", message)
            self.collection_status_label.setText(message)
            self.collection_status_label.setStyleSheet("color:#ef6c00; font-weight:600;")
            return

        self.calibration_collecting = False
        self.start_collection_button.setEnabled(True)
        self.finish_collection_button.setEnabled(False)
        self.minimum_raw_mm_spin.setValue(summary.minimum_raw_m * 1000.0)
        self.maximum_raw_mm_spin.setValue(summary.maximum_raw_m * 1000.0)
        if not self.calculate_two_point_calibration(show_dialog=True):
            return
        self.collection_status_label.setText(
            f"采集完成：{summary.sample_count} 帧、{summary.cycle_count} 个完整周期；"
            "已写入稳健最小/最大 X 轴宽度"
        )
        self.collection_status_label.setStyleSheet("color:#2e7d32; font-weight:600;")

    def calculate_two_point_calibration(
        self,
        _checked: bool = False,
        show_dialog: bool = True,
    ) -> bool:
        minimum_raw_m = self._raw_spin_m(self.minimum_raw_mm_spin)
        maximum_raw_m = self._raw_spin_m(self.maximum_raw_mm_spin)
        if minimum_raw_m is None or maximum_raw_m is None:
            message = "请先分别点击“记录当前最小点”和“记录当前最大点”，或手工填写两个原始 X 轴宽度。"
            if show_dialog:
                QMessageBox.warning(self, "标定点不完整", message)
            self.config_status.setText(message)
            self.config_status.setStyleSheet("color:#ef6c00;")
            return False
        try:
            self.distance_scale, self.distance_offset_m = calculate_distance_calibration(
                minimum_raw_m,
                self.minimum_gap_mm_spin.value() / 1000.0,
                maximum_raw_m,
                self.maximum_gap_mm_spin.value() / 1000.0,
            )
        except ValueError as exc:
            if show_dialog:
                QMessageBox.warning(self, "无法计算两点标定", str(exc))
            self.config_status.setText(f"标定失败：{exc}")
            self.config_status.setStyleSheet("color:#c62828;")
            return False
        self._refresh_calibration_result()
        self.config_status.setText("两点标定已计算；点击“保存并应用”后逐帧输出新的实际开口")
        self.config_status.setStyleSheet("color:#1565c0;")
        return True

    def update_live_result(self, result: dict) -> None:
        status = str(result.get("status", "--"))
        measurement = result.get("measurement") or {}
        depths = measurement.get("marker_depth_m") or {}
        self.last_marker_depths_m = {
            str(marker_id): float(value)
            for marker_id, value in depths.items()
            if isinstance(value, (int, float)) and np.isfinite(value) and value > 0.0
        }
        nominal_depth_m = measurement.get("nominal_marker_depth_m")
        depth_tolerance_m = measurement.get("marker_depth_tolerance_m")
        allowed_depth_text = ""
        if isinstance(nominal_depth_m, (int, float)) and isinstance(
            depth_tolerance_m, (int, float)
        ):
            minimum_depth_mm = (float(nominal_depth_m) - float(depth_tolerance_m)) * 1000.0
            maximum_depth_mm = (float(nominal_depth_m) + float(depth_tolerance_m)) * 1000.0
            allowed_depth_text = f"；允许 {minimum_depth_mm:.1f}–{maximum_depth_mm:.1f} mm"
        translations = {
            "tracking_gripper_distance": "正在逐帧测量",
            "insufficient_markers_for_distance": "必须同时看到两个标记",
            "marker_depth_out_of_range": "标记深度超出允许范围",
            "missing_intrinsics": "缺少相机内参；请使用 APV2",
            "no_markers": "未检测到配置的两个 ID",
            "processor_error": "处理错误",
        }
        status_detail = allowed_depth_text if status == "marker_depth_out_of_range" else ""
        self.live_status_label.setText(
            f"{translations.get(status, status)}{status_detail}（{status}）"
        )
        color = "#2e7d32" if status == "tracking_gripper_distance" else "#ef6c00"
        if status in {"processor_error", "missing_intrinsics", "marker_depth_out_of_range"}:
            color = "#c62828"
        self.live_status_label.setStyleSheet(f"color:{color}; font-weight:600;")

        ids = result.get("detected_ids") or []
        errors = [
            marker.get("reprojection_error_px")
            for marker in (result.get("markers") or {}).values()
            if isinstance(marker, dict) and marker.get("reprojection_error_px") is not None
        ]
        error_text = f"{max(errors):.3f} px" if errors else "--"
        output_error = result.get("output_error")
        output_text = (
            f"发送错误：{output_error}"
            if output_error
            else f"{self.output_host_edit.text().strip()}:{self.output_port_spin.value()}"
        )
        depth_text = ", ".join(
            f"ID {marker_id}={float(value) * 1000.0:.2f} mm"
            for marker_id, value in depths.items()
            if isinstance(value, (int, float))
        )
        self.live_detail_label.setText(
            f"检测 ID：{', '.join(str(value) for value in ids) if ids else '--'}　"
            f"深度：{depth_text or '--'}{allowed_depth_text}　重投影误差：{error_text}　UDP：{output_text}"
        )

        distance = result.get("gripper_distance") or {}
        raw_m = distance.get("raw_marker_x_distance_m")
        if status == "tracking_gripper_distance" and isinstance(raw_m, (int, float)):
            self.last_raw_distance_m = float(raw_m)
            self.recent_raw_samples.append((time.monotonic(), self.last_raw_distance_m))
            self.current_raw_label.setText(f"{self.last_raw_distance_m * 1000.0:.4f} mm")
            if self.calibration_collecting:
                self.calibration_samples_m.append(self.last_raw_distance_m)
                self._update_collection_summary()
        else:
            self.last_raw_distance_m = None
            self.current_raw_label.setText("--")
        calibrated_mm = distance.get("calibrated_mm")
        filtered_mm = distance.get("filtered_mm")
        calibration_complete = distance.get("calibration_complete") is True
        self.current_gap_label.setText(
            f"{float(calibrated_mm):.4f} mm"
            if calibration_complete and isinstance(calibrated_mm, (int, float))
            else "未标定"
            if distance
            else "--"
        )
        self.filtered_gap_label.setText(
            f"{float(filtered_mm):.4f} mm"
            if calibration_complete and isinstance(filtered_mm, (int, float))
            else "未标定"
            if distance
            else "--"
        )
