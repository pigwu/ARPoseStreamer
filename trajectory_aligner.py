#!/usr/bin/env python3
"""
Standalone trajectory alignment visualizer
Aligns two trajectories by:
1. Forcing start points to coincide
2. Computing optimal rotation to maximize overlap
3. Forcing end points to coincide (if requested)
"""

import numpy as np
import pandas as pd
import pyqtgraph as pg
import pyqtgraph.opengl as gl
from PyQt6.QtWidgets import QApplication
import sys


def load_iphone_csv(path):
    """Load iPhone ARKit CSV data"""
    df = pd.read_csv(path)
    positions = df[['x', 'y', 'z']].values
    return positions


def load_robot_csv(path):
    """Load robot arm CSV data"""
    df = pd.read_csv(path)
    positions = df[['x', 'y', 'z']].values
    return positions


def rotation_matrix_from_axis_angle(axis, angle):
    """Create rotation matrix from axis-angle representation"""
    axis = axis / np.linalg.norm(axis)
    a = np.cos(angle / 2.0)
    b, c, d = -axis * np.sin(angle / 2.0)
    aa, bb, cc, dd = a * a, b * b, c * c, d * d
    bc, ad, ac, ab, bd, cd = b * c, a * d, a * c, a * b, b * d, c * d
    return np.array([
        [aa + bb - cc - dd, 2 * (bc + ad), 2 * (bd - ac)],
        [2 * (bc - ad), aa + cc - bb - dd, 2 * (cd + ab)],
        [2 * (bd + ac), 2 * (cd - ab), aa + dd - bb - cc]
    ])


def align_start_and_end(trajectory1, trajectory2):
    """
    Align two trajectories by forcing both start and end points to match.
    Uses Procrustes analysis with start/end point constraints.

    Args:
        trajectory1: Reference trajectory (N1, 3)
        trajectory2: Trajectory to align (N2, 3)

    Returns:
        Aligned trajectory2
    """
    if len(trajectory1) < 2 or len(trajectory2) < 2:
        return trajectory2

    # Step 1: Translate trajectory2 so its start matches trajectory1's start
    offset_start = trajectory1[0] - trajectory2[0]
    traj2_translated = trajectory2 + offset_start

    # Step 2: Compute rotation to align end points
    # Vector from start to end for both trajectories
    vec1 = trajectory1[-1] - trajectory1[0]
    vec2 = traj2_translated[-1] - traj2_translated[0]

    # Normalize vectors
    vec1_norm = vec1 / (np.linalg.norm(vec1) + 1e-10)
    vec2_norm = vec2 / (np.linalg.norm(vec2) + 1e-10)

    # Compute rotation axis and angle
    rotation_axis = np.cross(vec2_norm, vec1_norm)
    rotation_axis_norm = np.linalg.norm(rotation_axis)

    if rotation_axis_norm < 1e-6:
        # Vectors are parallel, no rotation needed
        # Just scale to match end points
        scale = np.linalg.norm(vec1) / (np.linalg.norm(vec2) + 1e-10)
        traj2_centered = traj2_translated - trajectory1[0]
        traj2_scaled = traj2_centered * scale + trajectory1[0]
        return traj2_scaled

    rotation_axis = rotation_axis / rotation_axis_norm
    rotation_angle = np.arccos(np.clip(np.dot(vec1_norm, vec2_norm), -1.0, 1.0))

    # Create rotation matrix
    R = rotation_matrix_from_axis_angle(rotation_axis, rotation_angle)

    # Apply rotation around start point
    traj2_centered = traj2_translated - trajectory1[0]
    traj2_rotated = (R @ traj2_centered.T).T + trajectory1[0]

    # Step 3: Scale to match end point distance
    vec2_rotated = traj2_rotated[-1] - trajectory1[0]
    scale = np.linalg.norm(vec1) / (np.linalg.norm(vec2_rotated) + 1e-10)

    traj2_final = (traj2_rotated - trajectory1[0]) * scale + trajectory1[0]

    return traj2_final


def align_trajectories_procrustes(trajectory1, trajectory2, use_full_trajectory=True):
    """
    Align trajectory2 to trajectory1 using Procrustes analysis.
    Forces start points to match, then finds optimal rotation.

    Args:
        trajectory1: Reference trajectory (N1, 3)
        trajectory2: Trajectory to align (N2, 3)
        use_full_trajectory: If True, use all points; if False, use first 100 points

    Returns:
        Aligned trajectory2
    """
    if len(trajectory1) < 2 or len(trajectory2) < 2:
        return trajectory2

    # Determine how many points to use for alignment
    if use_full_trajectory:
        n_align = min(len(trajectory1), len(trajectory2))
    else:
        n_align = min(100, len(trajectory1), len(trajectory2))

    traj1_subset = trajectory1[:n_align]
    traj2_subset = trajectory2[:n_align]

    # Center both trajectories at their start points
    traj1_centered = traj1_subset - traj1_subset[0]
    traj2_centered = traj2_subset - traj2_subset[0]

    # Compute optimal rotation using SVD (Kabsch algorithm)
    H = traj2_centered.T @ traj1_centered
    U, _, Vt = np.linalg.svd(H)
    R = Vt.T @ U.T

    # Ensure proper rotation (det(R) = 1)
    if np.linalg.det(R) < 0:
        Vt[-1, :] *= -1
        R = Vt.T @ U.T

    # Apply rotation to entire trajectory2
    traj2_rotated = (R @ trajectory2.T).T

    # Translate to align start points
    offset = trajectory1[0] - traj2_rotated[0]
    traj2_aligned = traj2_rotated + offset

    return traj2_aligned


def main():
    # Load data
    iphone_path = "uploads/20260511-225434/pose_csv__pose.csv"
    robot_path = "uploads/end_effector_pose_log (1).csv"

    print("Loading data...")
    iphone_pos = load_iphone_csv(iphone_path)
    robot_pos = load_robot_csv(robot_path)

    print(f"iPhone: {len(iphone_pos)} samples")
    print(f"  Start: {iphone_pos[0]}")
    print(f"  End: {iphone_pos[-1]}")
    print(f"Robot: {len(robot_pos)} samples")
    print(f"  Start: {robot_pos[0]}")
    print(f"  End: {robot_pos[-1]}")

    # Try different alignment methods
    print("\n=== Method 1: Procrustes (first 100 points) ===")
    robot_aligned_1 = align_trajectories_procrustes(iphone_pos, robot_pos, use_full_trajectory=False)
    print(f"Aligned start: {robot_aligned_1[0]}")
    print(f"Aligned end: {robot_aligned_1[-1]}")
    print(f"Start error: {np.linalg.norm(robot_aligned_1[0] - iphone_pos[0]):.6f} m")
    print(f"End error: {np.linalg.norm(robot_aligned_1[-1] - iphone_pos[-1]):.6f} m")

    print("\n=== Method 2: Procrustes (full trajectory) ===")
    robot_aligned_2 = align_trajectories_procrustes(iphone_pos, robot_pos, use_full_trajectory=True)
    print(f"Aligned start: {robot_aligned_2[0]}")
    print(f"Aligned end: {robot_aligned_2[-1]}")
    print(f"Start error: {np.linalg.norm(robot_aligned_2[0] - iphone_pos[0]):.6f} m")
    print(f"End error: {np.linalg.norm(robot_aligned_2[-1] - iphone_pos[-1]):.6f} m")

    print("\n=== Method 3: Start + End point alignment ===")
    robot_aligned_3 = align_start_and_end(iphone_pos, robot_pos)
    print(f"Aligned start: {robot_aligned_3[0]}")
    print(f"Aligned end: {robot_aligned_3[-1]}")
    print(f"Start error: {np.linalg.norm(robot_aligned_3[0] - iphone_pos[0]):.6f} m")
    print(f"End error: {np.linalg.norm(robot_aligned_3[-1] - iphone_pos[-1]):.6f} m")

    # Create visualization
    print("\nCreating visualization...")
    app = QApplication(sys.argv)

    view = gl.GLViewWidget()
    view.setWindowTitle('Trajectory Alignment Comparison')
    view.setCameraPosition(distance=0.01)
    view.show()

    # Add grid
    grid = gl.GLGridItem()
    grid.scale(0.001, 0.001, 0.001)
    view.addItem(grid)

    # Add axes
    axis = gl.GLAxisItem()
    axis.setSize(0.005, 0.005, 0.005)
    view.addItem(axis)

    # Plot iPhone trajectory (cyan)
    iphone_line = gl.GLLinePlotItem(
        pos=iphone_pos,
        color=(0.0, 0.85, 1.0, 1.0),
        width=3,
        antialias=True
    )
    view.addItem(iphone_line)

    # Plot original robot trajectory (red, thin)
    robot_original_line = gl.GLLinePlotItem(
        pos=robot_pos,
        color=(1.0, 0.0, 0.0, 0.3),
        width=1,
        antialias=True
    )
    view.addItem(robot_original_line)

    # Plot aligned robot trajectory (amber/orange)
    # Use method 3 (start + end alignment) as default
    robot_aligned_line = gl.GLLinePlotItem(
        pos=robot_aligned_3,
        color=(1.0, 0.72, 0.18, 1.0),
        width=3,
        antialias=True
    )
    view.addItem(robot_aligned_line)

    # Add start/end markers
    start_marker = gl.GLScatterPlotItem(
        pos=np.array([iphone_pos[0]]),
        color=(0.0, 1.0, 0.0, 1.0),
        size=10
    )
    view.addItem(start_marker)

    end_marker = gl.GLScatterPlotItem(
        pos=np.array([iphone_pos[-1]]),
        color=(1.0, 0.0, 1.0, 1.0),
        size=10
    )
    view.addItem(end_marker)

    print("\nVisualization ready!")
    print("Cyan = iPhone trajectory")
    print("Amber/Orange = Aligned robot trajectory")
    print("Red (faint) = Original robot trajectory")
    print("Green = Start point")
    print("Magenta = End point")

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
