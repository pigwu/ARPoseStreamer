#!/bin/bash
# Build script for ARPose desktop tools
# Creates standalone applications for macOS

set -euo pipefail
export QT_API=pyqt6

echo "Checking PyInstaller..."
if ! pip show pyinstaller > /dev/null 2>&1; then
    echo "Installing PyInstaller..."
    pip install pyinstaller
fi

build_desktop_tool() {
    local name="$1"
    local entry_point="$2"

    echo ""
    echo "Building ${name}..."
    pyinstaller --name "${name}" \
        --clean \
        --windowed \
        --onefile \
        --icon=Assets.xcassets/AppIcon.appiconset/Icon-1024.png \
        --add-data "requirements_visualizer.txt:." \
        --hidden-import="OpenGL" \
        --hidden-import="OpenGL.GL" \
        --hidden-import="OpenGL.GLU" \
        --hidden-import="OpenGL.GLUT" \
        --hidden-import="pyqtgraph.opengl" \
        --exclude-module PyQt5 \
        --exclude-module PySide2 \
        --exclude-module PySide6 \
        --exclude-module torch \
        --exclude-module pandas \
        --exclude-module matplotlib \
        --exclude-module IPython \
        --exclude-module jupyter \
        --exclude-module notebook \
        --exclude-module sympy \
        --exclude-module PIL \
        --exclude-module tkinter \
        "${entry_point}"
}

build_desktop_tool "ARPose Visualizer" "udp_pose_visualizer.py"
build_desktop_tool "ARPose Tracking Validator" "pose_tracking_validator.py"
build_desktop_tool "ARPose Packet Loss Monitor" "udp_packet_loss_monitor.py"
build_desktop_tool "ARPose Zarr Exporter" "zarr_exporter_ui.py"
build_desktop_tool "AnySkin UDP Monitor" "anyskin_udp_monitor.py"

echo ""
echo "Build complete!"
echo "Application locations:"
echo "  dist/ARPose Visualizer.app"
echo "  dist/ARPose Tracking Validator.app"
echo "  dist/ARPose Packet Loss Monitor.app"
echo "  dist/ARPose Zarr Exporter.app"
echo "  dist/AnySkin UDP Monitor.app"
echo ""
echo "You can now run the application by double-clicking the .app file"
