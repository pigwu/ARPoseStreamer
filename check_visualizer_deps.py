#!/usr/bin/env python3
"""
Dependency checker for ARPose Visualizer
Checks if all required packages are installed and compatible
"""

import sys

def check_dependencies():
    """Check if all required dependencies are available"""
    deps = {
        'PyQt6': 'PyQt6.QtWidgets',
        'pyqtgraph': 'pyqtgraph',
        'numpy': 'numpy',
        'pyserial': 'serial',
        'PyOpenGL': 'OpenGL'
    }

    all_ok = True
    print("Checking ARPose Visualizer dependencies...\n")

    for name, module in deps.items():
        try:
            mod = __import__(module)
            version = getattr(mod, '__version__', 'unknown')
            print(f"[OK]      {name:15} version {version}")
        except ImportError as e:
            print(f"[MISSING] {name:15} {e}")
            all_ok = False

    print()
    if all_ok:
        print("[SUCCESS] All dependencies are installed!")
        print("\nYou can now run: python udp_pose_visualizer.py")
        return True
    else:
        print("[ERROR] Some dependencies are missing.")
        print("\nPlease install them with:")
        print("  pip install -r requirements_visualizer.txt")
        return False

if __name__ == "__main__":
    success = check_dependencies()
    sys.exit(0 if success else 1)
