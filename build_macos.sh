#!/bin/bash
# Build script for ARPose Visualizer
# Creates a standalone application for macOS

echo "Checking PyInstaller..."
if ! pip show pyinstaller > /dev/null 2>&1; then
    echo "Installing PyInstaller..."
    pip install pyinstaller
fi

echo ""
echo "Building ARPose Visualizer..."
pyinstaller --name "ARPose Visualizer" \
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
    udp_pose_visualizer.py

echo ""
echo "Build complete!"
echo "Application location: dist/ARPose Visualizer.app"
echo ""
echo "You can now run the application by double-clicking the .app file"
