# Clean build script - creates minimal executable
# This script creates a clean virtual environment with only required dependencies

Write-Host "Creating clean virtual environment..."
python -m venv venv_build

Write-Host "`nActivating virtual environment..."
& .\venv_build\Scripts\Activate.ps1

Write-Host "`nInstalling only required dependencies..."
pip install PyQt6 pyqtgraph numpy PyOpenGL pyinstaller

Write-Host "`nBuilding executable..."
pyinstaller --name "ARPose Visualizer" `
    --windowed `
    --onefile `
    --icon=Assets.xcassets/AppIcon.appiconset/Icon-1024.png `
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

Write-Host "`nCleaning up..."
deactivate
Remove-Item -Recurse -Force venv_build

Write-Host "`nBuild complete!"
Write-Host "Executable location: dist\ARPose Visualizer.exe"
