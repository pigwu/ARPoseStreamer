# Build script for ARPose desktop tools
# Creates standalone executables for Windows

$ErrorActionPreference = "Stop"
$env:QT_API = "pyqt6"

# Install PyInstaller if not already installed
Write-Host "Checking PyInstaller..."
pip show pyinstaller > $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyInstaller..."
    pip install pyinstaller
}

function Build-DesktopTool {
    param(
        [Parameter(Mandatory=$true)] [string] $Name,
        [Parameter(Mandatory=$true)] [string] $EntryPoint
    )

    Write-Host "`nBuilding $Name..."
    pyinstaller --name $Name `
        --clean `
        --windowed `
        --onefile `
        --icon=Assets.xcassets/AppIcon.appiconset/Icon-1024.png `
        --add-data "requirements_visualizer.txt;." `
        --hidden-import="OpenGL" `
        --hidden-import="OpenGL.GL" `
        --hidden-import="OpenGL.GLU" `
        --hidden-import="OpenGL.GLUT" `
        --hidden-import="pyqtgraph.opengl" `
        --exclude-module PyQt5 `
        --exclude-module PySide2 `
        --exclude-module PySide6 `
        --exclude-module torch `
        --exclude-module pandas `
        --exclude-module matplotlib `
        --exclude-module IPython `
        --exclude-module jupyter `
        --exclude-module notebook `
        --exclude-module sympy `
        --exclude-module PIL `
        --exclude-module tkinter `
        $EntryPoint

    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed while building $Name"
    }
}

# Build the executables
Build-DesktopTool -Name "ARPose Visualizer" -EntryPoint "udp_pose_visualizer.py"
Build-DesktopTool -Name "ARPose Tracking Validator" -EntryPoint "pose_tracking_validator.py"
Build-DesktopTool -Name "ARPose Packet Loss Monitor" -EntryPoint "udp_packet_loss_monitor.py"
Build-DesktopTool -Name "ARPose Zarr Exporter" -EntryPoint "zarr_exporter_ui.py"
Build-DesktopTool -Name "AnySkin UDP Monitor" -EntryPoint "anyskin_udp_monitor.py"

Write-Host "`nBuild complete!"
Write-Host "Executable locations:"
Write-Host "  dist\ARPose Visualizer.exe"
Write-Host "  dist\ARPose Tracking Validator.exe"
Write-Host "  dist\ARPose Packet Loss Monitor.exe"
Write-Host "  dist\ARPose Zarr Exporter.exe"
Write-Host "  dist\AnySkin UDP Monitor.exe"
Write-Host "`nYou can now run the application by double-clicking the .exe file"
