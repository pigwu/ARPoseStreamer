#!/usr/bin/env python3
"""
Export calibration as 4x4 transformation matrix
"""

import json
import numpy as np
from pathlib import Path


def quaternion_to_rotation_matrix(q):
    """Convert quaternion [qx, qy, qz, qw] to 3x3 rotation matrix"""
    qx, qy, qz, qw = q

    R = np.array([
        [1 - 2*(qy**2 + qz**2), 2*(qx*qy - qw*qz), 2*(qx*qz + qw*qy)],
        [2*(qx*qy + qw*qz), 1 - 2*(qx**2 + qz**2), 2*(qy*qz - qw*qx)],
        [2*(qx*qz - qw*qy), 2*(qy*qz + qw*qx), 1 - 2*(qx**2 + qy**2)]
    ])

    return R


def pose_to_matrix(position, quaternion):
    """Convert position + quaternion to 4x4 transformation matrix"""
    R = quaternion_to_rotation_matrix(quaternion)

    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = position

    return T


def calibration_to_matrix(calibration_file):
    """Load calibration.json and convert to 4x4 matrix"""
    with open(calibration_file, 'r') as f:
        calib = json.load(f)

    transform = calib.get('transform', {})
    scale = transform.get('scale', 1.0)
    rotation = np.array(transform.get('rotation', np.eye(3).tolist()))
    translation = np.array(transform.get('translation', [0, 0, 0]))

    # Build 4x4 matrix: T = [s*R | t]
    #                       [0   | 1]
    T = np.eye(4)
    T[:3, :3] = scale * rotation
    T[:3, 3] = translation

    return T


def main():
    # Load calibration
    calib_file = Path("calibration.json")
    if not calib_file.exists():
        print("Error: calibration.json not found")
        print("Please run pose_tracking_validator.py and save calibration first")
        return

    T = calibration_to_matrix(calib_file)

    print("Calibration as 4x4 Transformation Matrix:")
    print("=" * 50)
    print("Transform from Robot Arm to iPhone coordinate system:")
    print()
    print(T)
    print()
    print("Usage:")
    print("  iPhone_pose = T @ RobotArm_pose")
    print()

    # Save to file
    output_file = Path("calibration_matrix.txt")
    with output_file.open('w') as f:
        f.write("# Calibration Transformation Matrix (Robot Arm -> iPhone)\n")
        f.write("# Usage: iPhone_pose = T @ RobotArm_pose\n\n")
        np.savetxt(f, T, fmt='%.10f')

    print(f"Matrix saved to: {output_file}")

    # Also save as numpy binary
    np.save("calibration_matrix.npy", T)
    print(f"Binary saved to: calibration_matrix.npy")


if __name__ == "__main__":
    main()
