# Build script for ARPose Visualizer
# Creates a standalone executable for Windows

# Install PyInstaller if not already installed
Write-Host "Checking PyInstaller..."
pip show pyinstaller > $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyInstaller..."
    pip install pyinstaller
}

# Build the executable
Write-Host "`nBuilding ARPose Visualizer..."
pyinstaller --name "ARPose Visualizer" `
    --windowed `
    --onefile `
    --icon=Assets.xcassets/AppIcon.appiconset/Icon-1024.png `
    --add-data "requirements_visualizer.txt;." `
    --hidden-import="OpenGL" `
    --hidden-import="OpenGL.GL" `
    --hidden-import="OpenGL.GLU" `
    --hidden-import="OpenGL.GLUT" `
    --hidden-import="pyqtgraph.opengl" `
    --exclude-module torch `
    --exclude-module pandas `
    --exclude-module scipy `
    --exclude-module matplotlib `
    --exclude-module IPython `
    --exclude-module jupyter `
    --exclude-module notebook `
    --exclude-module sympy `
    --exclude-module PIL `
    --exclude-module tkinter `
    udp_pose_visualizer.py

Write-Host "`nBuild complete!"
Write-Host "Executable location: dist\ARPose Visualizer.exe"
Write-Host "`nYou can now run the application by double-clicking the .exe file"
