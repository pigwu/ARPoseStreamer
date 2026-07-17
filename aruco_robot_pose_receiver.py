from __future__ import annotations

import argparse
import json
import math
import socket
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Receive and validate AGP1 per-frame gripper distance.")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5570)
    parser.add_argument("--min-gap-mm", type=float, default=0.0)
    parser.add_argument("--max-gap-mm", type=float, default=200.0)
    parser.add_argument("--max-step-mm", type=float, default=20.0)
    return parser.parse_args()


def extract_safe_gripper_distance(
    message: dict[str, Any],
    previous_distance_mm: float | None,
    min_gap_mm: float,
    max_gap_mm: float,
    max_step_mm: float,
) -> float:
    if message.get("protocol") != "AGP1":
        raise ValueError("unsupported protocol")
    if message.get("status") != "tracking_gripper_distance":
        raise ValueError(f"distance is not valid: {message.get('status')}")
    distance = message.get("gripper_distance") or {}
    if distance.get("calibration_complete") is not True:
        raise ValueError("two-point gripper calibration is incomplete")
    value = distance.get("filtered_mm", distance.get("calibrated_mm"))
    try:
        gap_mm = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("gripper distance is missing") from exc
    if not math.isfinite(gap_mm):
        raise ValueError("gripper distance is non-finite")
    if not min_gap_mm <= gap_mm <= max_gap_mm:
        raise ValueError("gripper distance is outside configured limits")
    if previous_distance_mm is not None and abs(gap_mm - previous_distance_mm) > max_step_mm:
        raise ValueError("gripper distance jump exceeds max-step-mm")
    return gap_mm


def on_valid_distance(distance_mm: float) -> None:
    """Replace this print with your recorder/controller callback if needed."""
    print(f"VALID gripper_distance={distance_mm:.4f} mm")


def main() -> int:
    args = parse_args()
    previous_distance_mm = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.bind, args.port))
    print(f"Listening for AGP1 gripper distance on {args.bind}:{args.port}")
    try:
        while True:
            payload, _address = sock.recvfrom(65535)
            try:
                message = json.loads(payload.decode("utf-8"))
                distance_mm = extract_safe_gripper_distance(
                    message,
                    previous_distance_mm,
                    args.min_gap_mm,
                    args.max_gap_mm,
                    args.max_step_mm,
                )
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                print(f"REJECT {exc}")
                continue
            previous_distance_mm = distance_mm
            on_valid_distance(distance_mm)
    except KeyboardInterrupt:
        return 0
    finally:
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
