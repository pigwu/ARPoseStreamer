from __future__ import annotations

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
    TrackerConfig,
    calculate_distance_calibration,
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
        self.distance_scale = 1.0
        self.distance_offset_m = 0.0
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
        spin.setToolTip("可点击“记录当前”，也可以直接填写已记录的原始标记中心距离")
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
            "用途：逐帧测量夹爪开口。把 ID 0、ID 1 分别贴在两个活动夹爪上，"
            "然后记录最小点和最大点即可。无需机械臂型号、TCP 或 base/world 外参。"
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
                ("标记中心原始距离", self.current_raw_label),
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
        for column, (label, widget, tip) in enumerate(
            [
                ("字典", self.dictionary_combo, "随附 PDF 使用 DICT_4X4_50"),
                ("黑色外边长", self.marker_size_mm, "打印后实测应为 16.000 mm"),
                ("两个标记 ID", self.marker_ids_edit, "默认 0,1；顺序不影响欧氏距离"),
            ]
        ):
            marker_layout.addWidget(QLabel(label), 0, column)
            marker_layout.addWidget(widget, 1, column)
            widget.setToolTip(tip)
        layout.addWidget(marker_box)

        calibration_box = QGroupBox("2. 最小/最大两点标定")
        calibration = QGridLayout(calibration_box)
        calibration.addWidget(QLabel("位置"), 0, 0)
        calibration.addWidget(QLabel("实际夹爪开口（卡尺测量）"), 0, 1)
        calibration.addWidget(QLabel("原始标记中心距离"), 0, 2)

        self.minimum_gap_mm_spin = self._double_spin(0.0, 2000.0, 3, 0.1, " mm")
        self.maximum_gap_mm_spin = self._double_spin(0.0, 2000.0, 3, 0.1, " mm")
        self.minimum_raw_mm_spin = self._raw_point_spin()
        self.maximum_raw_mm_spin = self._raw_point_spin()
        minimum_button = QPushButton("记录当前")
        maximum_button = QPushButton("记录当前")
        minimum_button.clicked.connect(lambda: self.record_current_point("minimum"))
        maximum_button.clicked.connect(lambda: self.record_current_point("maximum"))

        calibration.addWidget(QLabel("最小开口"), 1, 0)
        calibration.addWidget(self.minimum_gap_mm_spin, 1, 1)
        calibration.addWidget(self.minimum_raw_mm_spin, 1, 2)
        calibration.addWidget(minimum_button, 1, 3)
        calibration.addWidget(QLabel("最大开口"), 2, 0)
        calibration.addWidget(self.maximum_gap_mm_spin, 2, 1)
        calibration.addWidget(self.maximum_raw_mm_spin, 2, 2)
        calibration.addWidget(maximum_button, 2, 3)

        calculate_button = QPushButton("计算/更新两点标定")
        calculate_button.clicked.connect(self.calculate_two_point_calibration)
        calibration.addWidget(calculate_button, 3, 0, 1, 4)
        self.calibration_result_label = QLabel("尚未完成两点标定")
        self.calibration_result_label.setWordWrap(True)
        self.calibration_result_label.setStyleSheet(
            "padding:7px; background:#eceff1; border-radius:4px;"
        )
        calibration.addWidget(self.calibration_result_label, 4, 0, 1, 4)
        formula = QLabel(
            "计算公式：实际开口 = scale × 原始标记距离 + offset。"
            "两组实际开口和原始距离都会写入 JSON，之后仍可填写、修改和重新计算。"
        )
        formula.setWordWrap(True)
        formula.setStyleSheet("color:#546e7a;")
        calibration.addWidget(formula, 5, 0, 1, 4)
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
            calibration_min_raw_m=minimum_raw_m,
            calibration_min_gap_m=minimum_gap_m,
            calibration_max_raw_m=maximum_raw_m,
            calibration_max_gap_m=maximum_gap_m,
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

    def record_current_point(self, point: str) -> None:
        if self.last_raw_distance_m is None:
            QMessageBox.warning(self, "当前帧不可用", "必须在当前帧同时检测到两个标记后才能记录。")
            return
        target = self.minimum_raw_mm_spin if point == "minimum" else self.maximum_raw_mm_spin
        target.setValue(self.last_raw_distance_m * 1000.0)
        name = "最小点" if point == "minimum" else "最大点"
        self.config_status.setText(
            f"已记录{name}原始距离 {self.last_raw_distance_m * 1000.0:.4f} mm；"
            "完成两个点后点击“计算/更新两点标定”"
        )
        self.config_status.setStyleSheet("color:#1565c0;")
        if (
            self._raw_spin_m(self.minimum_raw_mm_spin) is not None
            and self._raw_spin_m(self.maximum_raw_mm_spin) is not None
            and self.maximum_gap_mm_spin.value() > self.minimum_gap_mm_spin.value()
        ):
            self.calculate_two_point_calibration(show_dialog=False)
        else:
            self._refresh_calibration_result()

    def calculate_two_point_calibration(
        self,
        _checked: bool = False,
        show_dialog: bool = True,
    ) -> bool:
        minimum_raw_m = self._raw_spin_m(self.minimum_raw_mm_spin)
        maximum_raw_m = self._raw_spin_m(self.maximum_raw_mm_spin)
        if minimum_raw_m is None or maximum_raw_m is None:
            message = "请先在最小开口和最大开口位置分别点击“记录当前”，或手工填写两个原始距离。"
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
        translations = {
            "tracking_gripper_distance": "正在逐帧测量",
            "insufficient_markers_for_distance": "必须同时看到两个标记",
            "missing_intrinsics": "缺少相机内参；请使用 APV2",
            "no_markers": "未检测到配置的两个 ID",
            "processor_error": "处理错误",
        }
        self.live_status_label.setText(f"{translations.get(status, status)}（{status}）")
        color = "#2e7d32" if status == "tracking_gripper_distance" else "#ef6c00"
        if status in {"processor_error", "missing_intrinsics"}:
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
        self.live_detail_label.setText(
            f"检测 ID：{', '.join(str(value) for value in ids) if ids else '--'}　"
            f"重投影误差：{error_text}　UDP：{output_text}"
        )

        distance = result.get("gripper_distance") or {}
        raw_m = distance.get("raw_marker_center_m")
        if status == "tracking_gripper_distance" and isinstance(raw_m, (int, float)):
            self.last_raw_distance_m = float(raw_m)
            self.current_raw_label.setText(f"{self.last_raw_distance_m * 1000.0:.4f} mm")
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
