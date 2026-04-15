# Contributing

Thanks for your interest in improving ARPoseStreamer.

This repository is meant to stay small, practical, and easy to adapt for lab and research workflows. Contributions that improve reliability, clarity, portability, and downstream usability are especially welcome.

## Good Contribution Areas

- ARKit pose extraction improvements
- UDP transport robustness
- receiver-side tooling
- ROS2 or robotics pipeline bridges
- documentation and setup guides
- debugging and validation helpers

## Before You Start

Please first check whether your change is:

- a bug fix
- a feature request
- a documentation improvement
- a portability improvement

If the change is non-trivial, opening an issue first is helpful.

## Development Notes

### iPhone App

The iPhone app is built from:

- `ARPositionApp.swift`
- `ContentView.swift`
- `PositionViewModel.swift`
- `ARPoseUDPSender.swift`

The Xcode project is generated from:

- `project.yml`

If you change project structure, regenerate the project with:

```bash
xcodegen generate
```

### Receiver

The host-side reference receiver is:

- `udp_pose_receiver.py`

Please keep it working on both:

- macOS
- Windows

## Coding Guidelines

- Keep the sender lightweight and practical.
- Avoid adding rendering or unnecessary UI complexity.
- Prefer simple packet formats and clear documentation.
- Do not commit personal signing files, provisioning profiles, or device-specific configuration.
- If you change packet format, update:
  - `README.md`
  - receiver code
  - any install or usage docs that mention the packet

## Pull Requests

When opening a pull request, please include:

- what changed
- why it changed
- how you tested it
- whether packet format or installation steps changed

Helpful validation examples:

- receiver script runs on macOS
- receiver script runs on Windows
- Xcode project still generates with `xcodegen`
- app still builds and streams pose successfully

## Bug Reports

For bugs, please include as much of the following as possible:

- host OS and version
- iPhone model
- iOS version
- Xcode version
- whether you used personal-team signing or developer-program signing
- exact command used for the receiver
- logs or screenshots

## Style For Docs

- Prefer short, direct steps.
- Assume users may not be iOS experts.
- Keep setup instructions actionable and copy-paste friendly.

## License

By contributing, you agree that your contributions will be licensed under the MIT License used by this repository.
