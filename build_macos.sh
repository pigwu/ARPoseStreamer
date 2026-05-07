#!/bin/bash
# Build script for ARPose desktop tools
# Creates standalone applications for macOS

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
        --windowed \
        --onefile \
        --icon=Assets.xcassets/AppIcon.appiconset/Icon-1024.png \
        --add-data "requirements_visualizer.txt:." \
        --hidden-import="OpenGL" \
        --hidden-import="OpenGL.GL" \
        --hidden-import="OpenGL.GLU" \
        --hidden-import="OpenGL.GLUT" \
        --hidden-import="pyqtgraph.opengl" \
        --exclude-module torch \
        --exclude-module pandas \
        --exclude-module scipy \
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

echo ""
echo "Build complete!"
echo "Application locations:"
echo "  dist/ARPose Visualizer.app"
echo "  dist/ARPose Tracking Validator.app"
echo ""
echo "You can now run the application by double-clicking the .app file"
