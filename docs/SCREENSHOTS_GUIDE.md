# Screenshots Guide for ARPoseStreamer

To make the README more attractive, please capture the following screenshots:

## 1. PC Visualizer Screenshot
**File**: `docs/screenshots/visualizer-main.png`
**What to capture**: 
- The 3D visualizer window showing a trajectory
- Make sure the control panel is visible (IP address, checkboxes, stats)
- Capture when there's an active trajectory with the gradient colors visible

## 2. iPhone App Screenshot
**File**: `docs/screenshots/iphone-app.png`
**What to capture**:
- The iPhone app main screen with AR camera view
- Show the overlay with coordinates and trajectory
- Capture during active streaming

## 3. iPhone Settings Screenshot
**File**: `docs/screenshots/iphone-settings.png`
**What to capture**:
- The settings panel showing IP configuration
- Show the receiver IP field and OS selection

## 4. Upload Server in Action
**File**: `docs/screenshots/upload-stats.png`
**What to capture**:
- The visualizer showing upload count incrementing
- Capture the moment when "Uploads: X" flashes in cyan

## 5. Past Recordings View (Optional)
**File**: `docs/screenshots/past-recordings.png`
**What to capture**:
- The iPhone app's Past Recordings screen
- Show the list of recorded sessions

## How to Take Screenshots

### Windows (PC Visualizer):
1. Run `dist/ARPose Visualizer.exe`
2. Enable UDP Receiver and Upload Server
3. Press `Win + Shift + S` to capture a region
4. Save as PNG in `docs/screenshots/`

### iPhone:
1. Open the ARPoseStreamer app
2. Press `Volume Up + Power Button` simultaneously
3. Transfer screenshots to PC via AirDrop or iCloud
4. Save in `docs/screenshots/`

## After Capturing Screenshots

Run this command to add them to git:
```bash
git add docs/screenshots/*.png
git commit -m "Add screenshots to README"
git push origin main
```

Then I'll update the README to include these images!
