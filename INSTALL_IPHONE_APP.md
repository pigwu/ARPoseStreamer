# Install The iPhone App

You need a Mac for the final iPhone install step because iOS apps must be built and signed with Xcode.

## What I Already Prepared

This repository already contains:

- Swift source files for the app
- `Info.plist`
- `project.yml` for generating an Xcode project with XcodeGen

## On Your Mac

### 1. Clone Or Copy The Repository To The Mac

For example:

```bash
~/Desktop/Umi-app
```

### 2. Install Xcode

Install Xcode from the Mac App Store, then open it once so it finishes setup.

### 3. Install XcodeGen

If you use Homebrew:

```bash
brew install xcodegen
```

### 4. Generate The Xcode Project

In Terminal on the Mac:

```bash
cd ~/Desktop/Umi-app
xcodegen generate
```

This should create:

```bash
ARPoseStreamer.xcodeproj
```

### 5. Open The Project

```bash
open ARPoseStreamer.xcodeproj
```

### 6. Set Signing

In Xcode:

1. Select the `ARPoseStreamer` project.
2. Select the `ARPoseStreamer` target.
3. Open `Signing & Capabilities`.
4. Choose your Apple ID team.
5. If needed, change the bundle identifier to something unique, for example:

```text
com.yourname.ARPoseStreamer
```

## On Your iPhone

### 7. Prepare The Device

1. Connect the iPhone to the Mac with a cable.
2. Tap `Trust This Computer` on the iPhone if prompted.
3. On iOS 16+, enable `Developer Mode` if prompted.

### 8. Build And Install

In Xcode:

1. Choose your connected iPhone as the run destination.
2. Press the Run button.
3. The app will install on the iPhone.

If this is your first time using a personal Apple ID for sideloading, you may need to trust the developer profile on the phone:

`Settings > General > VPN & Device Management`

## First Launch Permissions

When you open the app, allow:

- camera access
- local network access

## Important Notes

- A free Apple ID usually works for direct personal-device installation, but the app signing may expire after 7 days.
- Without a Mac, I can still prepare source code and project files, but I cannot complete the actual iPhone installation step from here.
