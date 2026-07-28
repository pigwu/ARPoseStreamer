import os
import subprocess
import sys
from pathlib import Path

import zarr
from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from export_capture_to_zarr import (
    build_episode,
    default_eef_calibration_result,
    discover_capture,
    make_zarr_attrs,
    write_zarr,
)


class ExportWorker(QThread):
    log_message = pyqtSignal(str)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, captures, output_path, image_size, action_source, overwrite, eef_calibration_result=None):
        super().__init__()
        self.captures = [Path(path) for path in captures]
        self.output_path = Path(output_path)
        self.image_size = int(image_size)
        self.action_source = action_source
        self.overwrite = bool(overwrite)
        self.eef_calibration_result = (
            Path(eef_calibration_result).expanduser().resolve()
            if eef_calibration_result is not None
            else default_eef_calibration_result()
        )

    def run(self):
        try:
            if self.output_path.exists():
                if not self.overwrite:
                    raise FileExistsError(f"Output already exists: {self.output_path}")
                self.log_message.emit(f"Removing existing output: {self.output_path}")
                remove_tree(self.output_path)

            episodes = []
            for index, capture_path in enumerate(self.captures, start=1):
                self.log_message.emit(f"[{index}/{len(self.captures)}] Reading {capture_path}")
                capture = discover_capture(capture_path)
                episode = build_episode(
                    capture,
                    image_size=self.image_size,
                    action_source=self.action_source,
                    eef_calibration_result=self.eef_calibration_result,
                )
                episodes.append(episode)
                self.log_message.emit(
                    f"  frames={len(episode.timestamp)} "
                    f"force_valid={int(episode.force_valid.sum())}"
                )

            self.log_message.emit(f"Writing Zarr dataset: {self.output_path}")
            attrs = make_zarr_attrs(self.eef_calibration_result, action_source=self.action_source)
            attrs["created_by"] = "ARPose Zarr Exporter"
            write_zarr(self.output_path, episodes, attrs=attrs)
            total_frames = sum(len(episode.timestamp) for episode in episodes)
            self.log_message.emit(f"Done. episodes={len(episodes)} frames={total_frames}")
            self.finished_ok.emit(str(self.output_path))
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = None

        self.setWindowTitle("ARPose Zarr Exporter")
        self.resize(900, 640)

        self.capture_list = QListWidget()
        self.output_edit = QLineEdit(str(Path.cwd() / "dataset.zarr"))
        self.image_size_spin = QSpinBox()
        self.image_size_spin.setRange(64, 1024)
        self.image_size_spin.setSingleStep(32)
        self.image_size_spin.setValue(224)

        self.action_combo = QComboBox()
        self.action_combo.addItem("Next sampled pose action (RDP)", "next_obs")
        self.action_combo.addItem("Zero action", "zero")
        self.action_combo.addItem("Copy force into action[:,0:6]", "force")

        self.overwrite_check = QCheckBox("Overwrite existing output")
        self.overwrite_check.setChecked(True)

        self.convert_button = QPushButton("Convert")
        self.open_output_button = QPushButton("Open Output Folder")
        self.open_output_button.setEnabled(False)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)

        self.build_layout()
        self.connect_signals()
        self.apply_styles()

    def build_layout(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        title = QLabel("ARPose Zarr Exporter")
        title.setObjectName("TitleLabel")
        layout.addWidget(title)

        subtitle = QLabel("Convert ARPoseStreamer capture folders into dataset.zarr format.")
        subtitle.setObjectName("SubtitleLabel")
        layout.addWidget(subtitle)

        layout.addWidget(section_label("Capture folders"))
        layout.addWidget(self.capture_list, stretch=1)

        capture_buttons = QHBoxLayout()
        self.add_capture_button = QPushButton("Add Capture Folder")
        self.remove_capture_button = QPushButton("Remove Selected")
        self.clear_capture_button = QPushButton("Clear")
        capture_buttons.addWidget(self.add_capture_button)
        capture_buttons.addWidget(self.remove_capture_button)
        capture_buttons.addWidget(self.clear_capture_button)
        capture_buttons.addStretch()
        layout.addLayout(capture_buttons)

        layout.addWidget(horizontal_rule())

        layout.addWidget(section_label("Output"))
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit, stretch=1)
        self.browse_output_button = QPushButton("Browse")
        output_row.addWidget(self.browse_output_button)
        layout.addLayout(output_row)

        options_row = QHBoxLayout()
        options_row.addWidget(QLabel("Image size"))
        options_row.addWidget(self.image_size_spin)
        options_row.addSpacing(16)
        options_row.addWidget(QLabel("Action"))
        options_row.addWidget(self.action_combo)
        options_row.addSpacing(16)
        options_row.addWidget(self.overwrite_check)
        options_row.addStretch()
        layout.addLayout(options_row)

        action_row = QHBoxLayout()
        action_row.addWidget(self.convert_button)
        action_row.addWidget(self.open_output_button)
        action_row.addStretch()
        layout.addLayout(action_row)

        layout.addWidget(self.progress_bar)

        layout.addWidget(section_label("Log"))
        layout.addWidget(self.log_view, stretch=1)

        self.setCentralWidget(root)

    def connect_signals(self):
        self.add_capture_button.clicked.connect(self.add_capture_folder)
        self.remove_capture_button.clicked.connect(self.remove_selected_capture)
        self.clear_capture_button.clicked.connect(self.capture_list.clear)
        self.browse_output_button.clicked.connect(self.browse_output)
        self.convert_button.clicked.connect(self.start_conversion)
        self.open_output_button.clicked.connect(self.open_output_folder)

    def add_capture_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select capture folder", str(Path.cwd()))
        if not folder:
            return
        existing = {self.capture_list.item(i).text() for i in range(self.capture_list.count())}
        if folder not in existing:
            self.capture_list.addItem(folder)

    def remove_selected_capture(self):
        for item in self.capture_list.selectedItems():
            self.capture_list.takeItem(self.capture_list.row(item))

    def browse_output(self):
        start_dir = str(Path(self.output_edit.text()).parent if self.output_edit.text() else Path.cwd())
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Choose output Zarr directory",
            str(Path(start_dir) / "dataset.zarr"),
            "Zarr directories (*.zarr);;All files (*)",
        )
        if file_path:
            if not file_path.lower().endswith(".zarr"):
                file_path += ".zarr"
            self.output_edit.setText(file_path)

    def start_conversion(self):
        captures = [self.capture_list.item(i).text() for i in range(self.capture_list.count())]
        output_path = self.output_edit.text().strip()

        if not captures:
            QMessageBox.warning(self, "Missing input", "Add at least one capture folder.")
            return
        if not output_path:
            QMessageBox.warning(self, "Missing output", "Choose an output dataset.zarr path.")
            return

        self.set_running(True)
        self.log_view.clear()
        self.append_log("Starting conversion...")

        self.worker = ExportWorker(
            captures=captures,
            output_path=output_path,
            image_size=self.image_size_spin.value(),
            action_source=self.action_combo.currentData(),
            overwrite=self.overwrite_check.isChecked(),
        )
        self.worker.log_message.connect(self.append_log)
        self.worker.finished_ok.connect(self.on_conversion_finished)
        self.worker.failed.connect(self.on_conversion_failed)
        self.worker.start()

    def on_conversion_finished(self, output_path):
        self.set_running(False)
        self.open_output_button.setEnabled(True)
        self.append_log("")
        self.append_log("Output summary:")
        self.append_log(make_zarr_summary(Path(output_path)))
        QMessageBox.information(self, "Export complete", f"Wrote:\n{output_path}")

    def on_conversion_failed(self, message):
        self.set_running(False)
        self.append_log(f"ERROR: {message}")
        QMessageBox.critical(self, "Export failed", message)

    def open_output_folder(self):
        output_path = Path(self.output_edit.text().strip())
        target = output_path if output_path.is_dir() else output_path.parent
        if not target.exists():
            QMessageBox.warning(self, "Missing output", f"Folder does not exist:\n{target}")
            return
        open_folder(target)

    def append_log(self, message):
        self.log_view.appendPlainText(message)

    def set_running(self, running):
        self.convert_button.setEnabled(not running)
        self.add_capture_button.setEnabled(not running)
        self.remove_capture_button.setEnabled(not running)
        self.clear_capture_button.setEnabled(not running)
        self.browse_output_button.setEnabled(not running)
        self.progress_bar.setRange(0, 0 if running else 1)
        if not running:
            self.progress_bar.setValue(0)

    def apply_styles(self):
        self.setStyleSheet(
            """
            QWidget {
                background: #f8fafc;
                color: #0f172a;
                font-size: 13px;
            }
            QLabel#TitleLabel {
                font-size: 24px;
                font-weight: 700;
            }
            QLabel#SubtitleLabel {
                color: #475569;
            }
            QLabel#SectionLabel {
                font-size: 14px;
                font-weight: 700;
                color: #1e293b;
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
            QLineEdit, QListWidget, QPlainTextEdit, QSpinBox, QComboBox {
                background: white;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 6px;
            }
            QFrame#Rule {
                background: #cbd5e1;
                min-height: 1px;
                max-height: 1px;
            }
            """
        )


def make_zarr_summary(output_path):
    root = zarr.open_group(str(output_path), mode="r")
    lines = [f"path: {output_path}"]
    for key in sorted(root["data"].array_keys()):
        arr = root["data"][key]
        lines.append(f"data/{key}: shape={arr.shape} dtype={arr.dtype} chunks={arr.chunks}")
    lines.append(f"meta/episode_ends: {root['meta']['episode_ends'][:].tolist()}")
    return "\n".join(lines)


def remove_tree(path):
    import shutil

    shutil.rmtree(path)


def open_folder(path):
    if sys.platform.startswith("win"):
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def section_label(text):
    label = QLabel(text)
    label.setObjectName("SectionLabel")
    return label


def horizontal_rule():
    rule = QFrame()
    rule.setObjectName("Rule")
    rule.setFrameShape(QFrame.Shape.HLine)
    return rule


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
