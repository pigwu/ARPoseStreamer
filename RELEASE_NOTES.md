# ARPose Visualizer v1.0.0

## Features

- Real-time 3D trajectory visualization
- Integrated upload server (no separate command needed)
- Local IP display with copy button
- Display modes: all history or last 5 seconds
- Time-gradient colors (cyan → red)
- Live stats: FPS, packet count, dropped packets, latency, uploads
- Dark theme UI

## Download

- **Windows**: Download `ARPose Visualizer.exe` (52MB)
- **macOS**: Build from source using `./build_macos.sh`

## Installation (Windows)

1. Download `ARPose Visualizer.exe`
2. Double-click to run (no installation needed)
3. If Windows Defender shows a warning, click "More info" → "Run anyway"

## Usage

1. Check "Enable UDP Receiver" to receive pose data
2. Check "Enable Upload Server" to receive file uploads from iPhone
3. Copy the displayed local IP and enter it in the iPhone app settings
4. Start streaming from iPhone

## Requirements

- No Python installation required
- Windows 10 or later

## What's New

- First standalone release
- Optimized executable size (52MB vs 2.6GB)
- Fixed upload server freeze issue
- Added real-time upload count display

## Manual Release Steps

1. Go to https://github.com/pigwu/ARPoseStreamer/releases/new
2. Tag version: `v1.0.0`
3. Release title: `ARPose Visualizer v1.0.0`
4. Copy the content above into the description
5. Upload file: `dist/ARPose Visualizer.exe`
6. Click "Publish release"
