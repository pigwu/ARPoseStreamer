# Building Standalone Executable

This guide explains how to build standalone executables for the ARPose desktop tools that can be distributed without requiring Python installation.

## Prerequisites

1. Python 3.8 or higher installed
2. All dependencies installed: `pip install -r requirements_visualizer.txt`
3. PyInstaller will be automatically installed by the build script

The build scripts force `QT_API=pyqt6` and exclude other Qt bindings so PyInstaller does not accidentally collect multiple Qt packages from a shared Python environment.

## Building on Windows

1. Open PowerShell in the project directory
2. Run the build script:
   ```powershell
   .\build_windows.ps1
   ```
3. The executables will be created at:
   - `dist\ARPose Visualizer.exe`
   - `dist\ARPose Tracking Validator.exe`
   - `dist\ARPose Packet Loss Monitor.exe`
   - `dist\ARPose Zarr Exporter.exe`
   - `dist\AnySkin UDP Monitor.exe`
   - `dist\AnySkin Serial Mapper.exe`
4. You can distribute these .exe files

## Building on macOS

1. Open Terminal in the project directory
2. Run the build script:
   ```bash
   ./build_macos.sh
   ```
3. The applications will be created at:
   - `dist/ARPose Visualizer.app`
   - `dist/ARPose Tracking Validator.app`
   - `dist/ARPose Packet Loss Monitor.app`
   - `dist/ARPose Zarr Exporter.app`
   - `dist/AnySkin UDP Monitor.app`
4. You can distribute these .app bundles

## Distribution

### Windows
- The .exe file is completely standalone
- No Python installation required on target machine
- File size: ~100-150 MB (includes Python runtime and all dependencies)

### macOS
- The .app bundle is completely standalone
- No Python installation required on target machine
- File size: ~100-150 MB (includes Python runtime and all dependencies)
- Note: Users may need to right-click and select "Open" the first time due to macOS security

## Troubleshooting

### Build fails with "module not found"
- Make sure all dependencies are installed: `pip install -r requirements_visualizer.txt`
- Try reinstalling PyInstaller: `pip install --upgrade pyinstaller`

### Executable is too large
- This is normal - it includes the entire Python runtime
- You can use `--onedir` instead of `--onefile` for faster startup (but multiple files)

### macOS: "App is damaged and can't be opened"
- This is a security warning for unsigned apps
- Users should right-click the app and select "Open"
- Or run: `xattr -cr "ARPose Visualizer.app"`

### Windows: Antivirus flags the executable
- This is a false positive common with PyInstaller
- You can submit the file to your antivirus vendor for whitelisting
- Or distribute the source code and let users run with Python

## Advanced Options

### Custom Icon
Edit the build script and change the `--icon` parameter to your own .ico (Windows) or .icns (macOS) file.

### Include Additional Files
Use `--add-data` to include additional files:
```
--add-data "myfile.txt;." (Windows)
--add-data "myfile.txt:." (macOS)
```

### Debug Mode
Remove `--windowed` to see console output for debugging.
