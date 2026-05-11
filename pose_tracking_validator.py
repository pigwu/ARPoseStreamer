import argparse
import bisect
import csv
import json
import math
import socket
import struct
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pyqtgraph.opengl as gl
from PyQt6.QtCore import QThread, QTimer, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


FLOAT32_PACKET = struct.Struct("<Id7f")
V2_PACKET = struct.Struct("<4sHHIdd7fI")
V2_MAGIC = b"APS2"


@dataclass(frozen=True)
class PoseSample:
    stream: str
    sequence: int
    sender_time: float
    recv_time: float
    position: np.ndarray
    quaternion: np.ndarray
    sensor_time: Optional[float] = None
    protocol_version: int = 1
    checksum_valid: Optional[bool] = None


def decode_packet(packet: bytes):
    if len(packet) == V2_PACKET.size and packet[:4] == V2_MAGIC:
        return decode_v2_packet(packet)

    if len(packet) != FLOAT32_PACKET.size:
        raise ValueError(f"Expected {FLOAT32_PACKET.size} bytes, got {len(packet)}")
    sequence, sender_time, x, y, z, qx, qy, qz, qw = FLOAT32_PACKET.unpack(packet)
    quat = normalize_quaternion(np.array([qx, qy, qz, qw], dtype=float))
    return sequence, sender_time, np.array([x, y, z], dtype=float), quat, None, 1, None


def decode_v2_packet(packet: bytes):
    payload = packet[:-4]
    checksum = struct.unpack("<I", packet[-4:])[0]
    checksum_valid = fnv1a_bytes(payload) == checksum
    magic, version, flags, sequence, sensor_time, received_time, x, y, z, qx, qy, qz, qw, _ = V2_PACKET.unpack(packet)
    if magic != V2_MAGIC:
        raise ValueError("Invalid APS2 packet magic")
    if not checksum_valid:
        raise ValueError("Invalid APS2 packet checksum")

    has_sensor_time = bool(flags & 1)
    quat = normalize_quaternion(np.array([qx, qy, qz, qw], dtype=float))
    return sequence, received_time, np.array([x, y, z], dtype=float), quat, sensor_time if has_sensor_time else None, version, checksum_valid


def fnv1a_bytes(payload):
    value = 2166136261
    for byte in payload:
        value ^= byte
        value = (value * 16777619) & 0xFFFFFFFF
    return value


def normalize_quaternion(quaternion):
    norm = float(np.linalg.norm(quaternion))
    if norm < 1e-9:
        return np.array([0.0, 0.0, 0.0, 1.0], dtype=float)
    return quaternion / norm


def quaternion_to_matrix(quaternion):
    x, y, z, w = normalize_quaternion(quaternion)
    return np.array(
        [
            [1 - 2 * y * y - 2 * z * z, 2 * x * y - 2 * z * w, 2 * x * z + 2 * y * w],
            [2 * x * y + 2 * z * w, 1 - 2 * x * x - 2 * z * z, 2 * y * z - 2 * x * w],
            [2 * x * z - 2 * y * w, 2 * y * z + 2 * x * w, 1 - 2 * x * x - 2 * y * y],
        ],
        dtype=float,
    )


def quaternion_angle_error_degrees(first, second):
    dot = abs(float(np.dot(normalize_quaternion(first), normalize_quaternion(second))))
    dot = max(-1.0, min(1.0, dot))
    return math.degrees(2.0 * math.acos(dot))


def quaternion_multiply(first, second):
    ax, ay, az, aw = first
    bx, by, bz, bw = second
    return normalize_quaternion(
        np.array(
            [
                aw * bx + ax * bw + ay * bz - az * by,
                aw * by - ax * bz + ay * bw + az * bx,
                aw * bz + ax * by - ay * bx + az * bw,
                aw * bw - ax * bx - ay * by - az * bz,
            ],
            dtype=float,
        )
    )


def quaternion_conjugate(quaternion):
    x, y, z, w = normalize_quaternion(quaternion)
    return np.array([-x, -y, -z, w], dtype=float)


def matrix_to_quaternion(matrix):
    trace = float(np.trace(matrix))
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        qw = 0.25 * scale
        qx = (matrix[2, 1] - matrix[1, 2]) / scale
        qy = (matrix[0, 2] - matrix[2, 0]) / scale
        qz = (matrix[1, 0] - matrix[0, 1]) / scale
    else:
        diagonal = np.diag(matrix)
        axis = int(np.argmax(diagonal))
        if axis == 0:
            scale = math.sqrt(1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2]) * 2.0
            qw = (matrix[2, 1] - matrix[1, 2]) / scale
            qx = 0.25 * scale
            qy = (matrix[0, 1] + matrix[1, 0]) / scale
            qz = (matrix[0, 2] + matrix[2, 0]) / scale
        elif axis == 1:
            scale = math.sqrt(1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2]) * 2.0
            qw = (matrix[0, 2] - matrix[2, 0]) / scale
            qx = (matrix[0, 1] + matrix[1, 0]) / scale
            qy = 0.25 * scale
            qz = (matrix[1, 2] + matrix[2, 1]) / scale
        else:
            scale = math.sqrt(1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1]) * 2.0
            qw = (matrix[1, 0] - matrix[0, 1]) / scale
            qx = (matrix[0, 2] + matrix[2, 0]) / scale
            qy = (matrix[1, 2] + matrix[2, 1]) / scale
            qz = 0.25 * scale
    return normalize_quaternion(np.array([qx, qy, qz, qw], dtype=float))


def average_quaternion(quaternions):
    if not quaternions:
        return np.array([0.0, 0.0, 0.0, 1.0], dtype=float)

    reference = normalize_quaternion(quaternions[0])
    accumulator = np.zeros(4, dtype=float)
    for quaternion in quaternions:
        aligned = normalize_quaternion(quaternion)
        if np.dot(reference, aligned) < 0.0:
            aligned = -aligned
        accumulator += aligned
    return normalize_quaternion(accumulator)


def nearest_by_sender_time(samples, target_time, max_delta_seconds):
    best = None
    best_delta = float("inf")
    for sample in samples:
        delta = abs(sample.sender_time - target_time)
        if delta < best_delta:
            best = sample
            best_delta = delta
    if best is None or best_delta > max_delta_seconds:
        return None, best_delta
    return best, best_delta


def load_pose_csv(path, stream):
    samples = []
    with Path(path).open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for index, row in enumerate(reader):
            try:
                sequence = int(float(row.get("sequence", index)))
                # Prefer relative_time for synchronization, fall back to absolute timestamps
                sender_time = first_float(row, ["relative_time", "time", "sender_time", "received_time", "timestamp"])
                frame_time = first_float(row, ["frame_time", "received_time", "recv_time"], default=sender_time)
                sensor_time = first_float(row, ["sensor_time"], default=None, required=False)
                position = np.array([
                    float(row["x"]),
                    float(row["y"]),
                    float(row["z"]),
                ], dtype=float)
                quaternion = normalize_quaternion(np.array([
                    float(row["qx"]),
                    float(row["qy"]),
                    float(row["qz"]),
                    float(row["qw"]),
                ], dtype=float))
            except (KeyError, TypeError, ValueError):
                continue

            samples.append(
                PoseSample(
                    stream=stream,
                    sequence=sequence,
                    sender_time=sender_time,
                    recv_time=frame_time,
                    position=position,
                    quaternion=quaternion,
                    sensor_time=sensor_time,
                    protocol_version=int(float(row.get("protocol_version", 1) or 1)),
                    checksum_valid=parse_bool(row.get("checksum_valid")),
                )
            )
    return samples


def first_float(row, keys, default=None, required=True):
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return float(value)
    if required and default is None:
        raise ValueError(f"Missing required numeric field from {keys}")
    return default


def parse_bool(value):
    if value is None or value == "":
        return None
    return str(value).strip().lower() in {"1", "true", "yes"}


class SimilarityTransform:
    def __init__(self, scale=1.0, rotation=None, translation=None, orientation_delta=None):
        self.scale = scale
        self.rotation = rotation if rotation is not None else np.eye(3, dtype=float)
        self.translation = translation if translation is not None else np.zeros(3, dtype=float)
        self.orientation_delta = orientation_delta if orientation_delta is not None else np.array([0.0, 0.0, 0.0, 1.0])

    def apply_position(self, position):
        return self.scale * (self.rotation @ position) + self.translation

    def apply_quaternion(self, quaternion):
        rotated = quaternion_multiply(matrix_to_quaternion(self.rotation), quaternion)
        return quaternion_multiply(self.orientation_delta, rotated)

    def to_dict(self):
        return {
            "scale": self.scale,
            "rotation": self.rotation.tolist(),
            "translation": self.translation.tolist(),
            "orientation_delta": self.orientation_delta.tolist(),
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            scale=float(data.get("scale", 1.0)),
            rotation=np.array(data.get("rotation", np.eye(3).tolist()), dtype=float),
            translation=np.array(data.get("translation", [0.0, 0.0, 0.0]), dtype=float),
            orientation_delta=np.array(data.get("orientation_delta", [0.0, 0.0, 0.0, 1.0]), dtype=float),
        )


class CalibrationResult:
    def __init__(self):
        self.enabled = False
        self.transform = SimilarityTransform()
        self.time_offset = 0.0
        self.time_slope = 1.0
        self.mean_time_error = None
        self.time_rmse = None
        self.max_time_error = None
        self.position_rmse = None
        self.angle_rmse = None
        self.pair_count = 0
        self.scale = 1.0
        self.quality = "waiting"
        self.motion_coverage = "none"
        self.inlier_ratio = 0.0

    def sensor_to_arkit_time(self, sensor_time):
        return self.time_slope * sensor_time + self.time_offset

    def arkit_to_sensor_time(self, arkit_time):
        if abs(self.time_slope) < 1e-9:
            return arkit_time - self.time_offset
        return (arkit_time - self.time_offset) / self.time_slope

    def to_dict(self):
        return {
            "enabled": self.enabled,
            "transform": self.transform.to_dict(),
            "time_offset": self.time_offset,
            "time_slope": self.time_slope,
            "mean_time_error": self.mean_time_error,
            "time_rmse": self.time_rmse,
            "max_time_error": self.max_time_error,
            "position_rmse": self.position_rmse,
            "angle_rmse": self.angle_rmse,
            "pair_count": self.pair_count,
            "scale": self.scale,
            "quality": self.quality,
            "motion_coverage": self.motion_coverage,
            "inlier_ratio": self.inlier_ratio,
        }

    @classmethod
    def from_dict(cls, data):
        result = cls()
        result.enabled = bool(data.get("enabled", True))
        result.transform = SimilarityTransform.from_dict(data.get("transform", {}))
        result.time_offset = float(data.get("time_offset", 0.0))
        result.time_slope = float(data.get("time_slope", 1.0))
        result.mean_time_error = data.get("mean_time_error")
        result.time_rmse = data.get("time_rmse")
        result.max_time_error = data.get("max_time_error")
        result.position_rmse = data.get("position_rmse")
        result.angle_rmse = data.get("angle_rmse")
        result.pair_count = int(data.get("pair_count", 0))
        result.scale = float(data.get("scale", result.transform.scale))
        result.quality = data.get("quality", "loaded")
        result.motion_coverage = data.get("motion_coverage", "loaded")
        result.inlier_ratio = float(data.get("inlier_ratio", 1.0))
        return result


class AdaptiveCalibrator:
    def __init__(self, max_time_offset=0.5, offset_step=0.02, pairing_window=0.04, min_pairs=20, min_motion_span=0.05):
        self.max_time_offset = max_time_offset
        self.offset_step = offset_step
        self.pairing_window = pairing_window
        self.min_pairs = min_pairs
        self.min_motion_span = min_motion_span
        self.result = CalibrationResult()

    def update(self, arkit_samples, sensor_samples):
        if len(arkit_samples) < self.min_pairs or len(sensor_samples) < self.min_pairs:
            self.result = CalibrationResult()
            return self.result

        arkit_list = list(arkit_samples)
        sensor_list = list(sensor_samples)
        candidate_offsets = np.arange(-self.max_time_offset, self.max_time_offset + self.offset_step * 0.5, self.offset_step)
        best = None

        for offset in candidate_offsets:
            pairs = self.make_pairs(arkit_list, sensor_list, time_offset=offset, time_slope=1.0)
            if len(pairs) < self.min_pairs:
                continue
            if not self.has_enough_motion(pairs):
                continue
            transform, position_rmse = self.estimate_similarity(pairs)
            score = self.normalized_position_score(pairs, transform, position_rmse)
            if best is None or score < best[0]:
                best = (score, offset, pairs, transform, position_rmse)

        if best is None:
            self.result = CalibrationResult()
            return self.result

        _, offset, pairs, transform, position_rmse = best
        time_slope, time_offset = self.estimate_time_model(pairs)
        pairs = self.make_pairs(arkit_list, sensor_list, time_offset=time_offset, time_slope=time_slope)
        if len(pairs) >= self.min_pairs:
            transform, position_rmse = self.estimate_similarity(pairs)
            pairs, inlier_ratio = self.filter_inliers(pairs, transform)
            if len(pairs) >= self.min_pairs:
                transform, position_rmse = self.estimate_similarity(pairs)
        else:
            time_slope = 1.0
            time_offset = offset
            inlier_ratio = 1.0

        angle_rmse = self.estimate_orientation_delta(transform, pairs)

        result = CalibrationResult()
        result.enabled = True
        result.transform = transform
        result.time_offset = float(time_offset)
        result.time_slope = float(time_slope)
        result.mean_time_error, result.time_rmse, result.max_time_error = self.time_error_stats(pairs, time_offset, time_slope)
        result.position_rmse = position_rmse
        result.angle_rmse = angle_rmse
        result.pair_count = len(pairs)
        result.scale = transform.scale
        result.motion_coverage = self.motion_coverage(pairs)
        result.inlier_ratio = inlier_ratio
        result.quality = self.quality_label(result)
        self.result = result
        return result

    def sample_time(self, sample):
        return sample.sensor_time if sample.sensor_time is not None else sample.sender_time

    def make_pairs(self, arkit_samples, sensor_samples, time_offset, time_slope=1.0):
        sensor_times = [time_slope * self.sample_time(sample) + time_offset for sample in sensor_samples]
        pairs = []
        for arkit in arkit_samples:
            index = bisect.bisect_left(sensor_times, arkit.sender_time)
            candidates = []
            if index < len(sensor_samples):
                candidates.append((abs(sensor_times[index] - arkit.sender_time), sensor_samples[index]))
            if index > 0:
                candidates.append((abs(sensor_times[index - 1] - arkit.sender_time), sensor_samples[index - 1]))
            if not candidates:
                continue
            delta, sensor = min(candidates, key=lambda item: item[0])
            if delta <= self.pairing_window:
                pairs.append((arkit, sensor))
        return pairs

    def estimate_time_model(self, pairs):
        sensor_times = np.array([self.sample_time(pair[1]) for pair in pairs], dtype=float)
        arkit_times = np.array([pair[0].sender_time for pair in pairs], dtype=float)
        if len(sensor_times) < 2 or float(np.ptp(sensor_times)) < 1e-6:
            return 1.0, float(np.mean(arkit_times - sensor_times))

        slope, intercept = np.polyfit(sensor_times, arkit_times, 1)
        slope = float(np.clip(slope, 0.98, 1.02))
        intercept = float(np.mean(arkit_times - slope * sensor_times))
        return slope, intercept

    def filter_inliers(self, pairs, transform):
        errors = np.array([
            np.linalg.norm(transform.apply_position(pair[1].position) - pair[0].position)
            for pair in pairs
        ], dtype=float)
        if len(errors) < self.min_pairs:
            return pairs, 1.0
        median = float(np.median(errors))
        mad = float(np.median(np.abs(errors - median)))
        threshold = median + max(0.03, 3.0 * 1.4826 * mad)
        inliers = [pair for pair, error in zip(pairs, errors) if error <= threshold]
        if len(inliers) < self.min_pairs:
            return pairs, 1.0
        return inliers, len(inliers) / len(pairs)

    def has_enough_motion(self, pairs):
        arkit_points = np.array([pair[0].position for pair in pairs], dtype=float)
        sensor_points = np.array([pair[1].position for pair in pairs], dtype=float)
        if len(arkit_points) < self.min_pairs:
            return False
        arkit_span = float(np.max(np.linalg.norm(arkit_points - arkit_points.mean(axis=0), axis=1)))
        sensor_span = float(np.max(np.linalg.norm(sensor_points - sensor_points.mean(axis=0), axis=1)))
        return arkit_span >= self.min_motion_span and sensor_span >= self.min_motion_span

    def normalized_position_score(self, pairs, transform, position_rmse):
        arkit_points = np.array([pair[0].position for pair in pairs], dtype=float)
        span = float(np.max(np.linalg.norm(arkit_points - arkit_points.mean(axis=0), axis=1)))
        return position_rmse / max(span, 1e-3)

    def estimate_similarity(self, pairs):
        arkit_points = np.array([pair[0].position for pair in pairs], dtype=float)
        sensor_points = np.array([pair[1].position for pair in pairs], dtype=float)
        sensor_mean = sensor_points.mean(axis=0)
        arkit_mean = arkit_points.mean(axis=0)
        sensor_centered = sensor_points - sensor_mean
        arkit_centered = arkit_points - arkit_mean

        covariance = sensor_centered.T @ arkit_centered / len(pairs)
        u, singular_values, vh = np.linalg.svd(covariance)
        correction = np.eye(3, dtype=float)
        if np.linalg.det(vh.T @ u.T) < 0:
            correction[2, 2] = -1.0
        rotation = vh.T @ correction @ u.T
        variance = float(np.mean(np.sum(sensor_centered * sensor_centered, axis=1)))
        scale = 1.0 if variance < 1e-9 else float(np.sum(singular_values * np.diag(correction)) / variance)
        translation = arkit_mean - scale * (rotation @ sensor_mean)

        transform = SimilarityTransform(scale=scale, rotation=rotation, translation=translation)
        aligned = np.array([transform.apply_position(point) for point in sensor_points], dtype=float)
        rmse = float(np.sqrt(np.mean(np.sum((aligned - arkit_points) ** 2, axis=1))))
        return transform, rmse

    def time_error_stats(self, pairs, offset, slope=1.0):
        errors = np.array([slope * self.sample_time(pair[1]) + offset - pair[0].sender_time for pair in pairs], dtype=float)
        mean_error = float(np.mean(errors))
        rmse = float(np.sqrt(np.mean(errors * errors)))
        max_error = float(np.max(np.abs(errors)))
        return mean_error, rmse, max_error

    def motion_coverage(self, pairs):
        arkit_points = np.array([pair[0].position for pair in pairs], dtype=float)
        if len(arkit_points) < 2:
            return "none"
        span = np.ptp(arkit_points, axis=0)
        axes = [name for name, value in zip(["x", "y", "z"], span) if value >= self.min_motion_span]
        return "none" if not axes else "".join(axes)

    def quality_label(self, result):
        if result.pair_count < self.min_pairs:
            return "waiting"
        coverage_count = 0 if result.motion_coverage == "none" else len(result.motion_coverage)
        position_ok = result.position_rmse is not None and result.position_rmse < 0.05
        timing_ok = result.time_rmse is not None and result.time_rmse < 0.02
        if coverage_count >= 3 and position_ok and timing_ok and result.inlier_ratio >= 0.75:
            return "good"
        if coverage_count >= 2 and result.inlier_ratio >= 0.5:
            return "weak"
        return "unstable"

    def estimate_orientation_delta(self, transform, pairs):
        rotation_quaternion = matrix_to_quaternion(transform.rotation)
        deltas = []
        errors = []

        for arkit, sensor in pairs:
            rotated_sensor = quaternion_multiply(rotation_quaternion, sensor.quaternion)
            delta = quaternion_multiply(arkit.quaternion, quaternion_conjugate(rotated_sensor))
            deltas.append(delta)

        transform.orientation_delta = average_quaternion(deltas)

        for arkit, sensor in pairs:
            aligned_sensor = transform.apply_quaternion(sensor.quaternion)
            errors.append(quaternion_angle_error_degrees(arkit.quaternion, aligned_sensor))

        if not errors:
            return None
        return float(np.sqrt(np.mean(np.square(errors))))


class PoseReceiverThread(QThread):
    sample_received = pyqtSignal(object)
    status_updated = pyqtSignal(str, dict)
    error_occurred = pyqtSignal(str, str)

    def __init__(self, stream_name, port, host="0.0.0.0"):
        super().__init__()
        self.stream_name = stream_name
        self.host = host
        self.port = port
        self.running = False
        self.prev_sequence = None
        self.prev_recv_time = None
        self.packet_count = 0
        self.drop_count = 0
        self.start_time = 0.0

    def run(self):
        self.running = True
        self.start_time = time.time()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        try:
            sock.bind((self.host, self.port))
            sock.settimeout(0.1)
            while self.running:
                try:
                    packet, address = sock.recvfrom(4096)
                except socket.timeout:
                    continue

                recv_time = time.time()
                monotonic_time = time.monotonic()

                try:
                    sequence, sender_time, position, quaternion, sensor_time, protocol_version, checksum_valid = decode_packet(packet)
                except Exception as exc:
                    self.error_occurred.emit(self.stream_name, str(exc))
                    continue

                if self.prev_sequence is not None:
                    self.drop_count += max(0, sequence - self.prev_sequence - 1)
                self.prev_sequence = sequence

                fps = 0.0
                if self.prev_recv_time is not None:
                    fps = 1.0 / max(monotonic_time - self.prev_recv_time, 1e-9)
                self.prev_recv_time = monotonic_time
                self.packet_count += 1

                sample = PoseSample(
                    stream=self.stream_name,
                    sequence=sequence,
                    sender_time=sender_time,
                    recv_time=recv_time,
                    position=position,
                    quaternion=quaternion,
                    sensor_time=sensor_time,
                    protocol_version=protocol_version,
                    checksum_valid=checksum_valid,
                )
                self.sample_received.emit(sample)
                self.status_updated.emit(
                    self.stream_name,
                    {
                        "address": f"{address[0]}:{address[1]}",
                        "fps": fps,
                        "packets": self.packet_count,
                        "drops": self.drop_count,
                        "latency_ms": max(0.0, (recv_time - sender_time) * 1000.0),
                        "protocol_version": protocol_version,
                    },
                )
        except Exception as exc:
            self.error_occurred.emit(self.stream_name, f"Socket error: {exc}")
        finally:
            sock.close()

    def stop(self):
        self.running = False


class PoseTrack:
    def __init__(self, max_samples=12000):
        self.samples = deque(maxlen=max_samples)
        self.origin = None

    def append(self, sample):
        if self.origin is None:
            self.origin = sample.position.copy()
        self.samples.append(sample)

    def reset_origin(self):
        if self.samples:
            self.origin = self.samples[-1].position.copy()

    def positions(self, last_seconds=None):
        if not self.samples:
            return np.empty((0, 3), dtype=float)

        # For CSV data, recv_time is relative time, not Unix timestamp
        # Only apply time filtering for live data (recv_time > 1e9 indicates Unix timestamp)
        cutoff = None
        if last_seconds is not None and self.samples and self.samples[0].recv_time > 1e9:
            cutoff = time.time() - last_seconds

        points = []
        origin = self.origin if self.origin is not None else np.zeros(3, dtype=float)
        for sample in self.samples:
            if cutoff is not None and sample.recv_time < cutoff:
                continue
            points.append(sample.position - origin)
        if not points:
            return np.empty((0, 3), dtype=float)
        return np.array(points, dtype=float)

    def latest_relative(self):
        if not self.samples:
            return None
        origin = self.origin if self.origin is not None else np.zeros(3, dtype=float)
        sample = self.samples[-1]
        return PoseSample(
            stream=sample.stream,
            sequence=sample.sequence,
            sender_time=sample.sender_time,
            recv_time=sample.recv_time,
            position=sample.position - origin,
            quaternion=sample.quaternion,
            sensor_time=sample.sensor_time,
            protocol_version=sample.protocol_version,
            checksum_valid=sample.checksum_valid,
        )


class CalibratedSensorTrack:
    def __init__(self, source_track, calibration_result):
        self.source_track = source_track
        self.calibration_result = calibration_result
        self.origin = None

    def positions(self, last_seconds=None, reference_origin=None):
        if not self.calibration_result.enabled:
            print(f"CalibratedSensorTrack.positions: calibration not enabled!")
            return np.empty((0, 3), dtype=float)

        if not self.source_track.samples:
            print(f"CalibratedSensorTrack.positions: no source samples!")
            return np.empty((0, 3), dtype=float)

        # For CSV data, recv_time is relative time, not Unix timestamp
        # Only apply time filtering for live data (recv_time > 1e9 indicates Unix timestamp)
        cutoff = None
        if last_seconds is not None and self.source_track.samples and self.source_track.samples[0].recv_time > 1e9:
            cutoff = time.time() - last_seconds

        points = []
        for sample in self.source_track.samples:
            if cutoff is not None and sample.recv_time < cutoff:
                continue
            points.append(self.calibration_result.transform.apply_position(sample.position))

        if not points:
            print(f"CalibratedSensorTrack.positions: no points after filtering!")
            return np.empty((0, 3), dtype=float)

        points = np.array(points, dtype=float)
        print(f"CalibratedSensorTrack.positions: returning {len(points)} points")
        if reference_origin is not None:
            return points - reference_origin
        if self.origin is None:
            self.origin = points[0].copy()
        return points - self.origin

    def latest_relative(self, reference_origin=None):
        if not self.calibration_result.enabled or not self.source_track.samples:
            return None

        sample = self.source_track.samples[-1]
        position = self.calibration_result.transform.apply_position(sample.position)
        if reference_origin is None and self.origin is None:
            self.origin = position.copy()
        origin = reference_origin if reference_origin is not None else self.origin

        return PoseSample(
            stream=sample.stream,
            sequence=sample.sequence,
            sender_time=sample.sender_time,
            recv_time=sample.recv_time,
            position=position - origin,
            quaternion=self.calibration_result.transform.apply_quaternion(sample.quaternion),
            sensor_time=sample.sensor_time,
            protocol_version=sample.protocol_version,
            checksum_valid=sample.checksum_valid,
        )

    def reset_origin(self):
        self.origin = None
        if self.calibration_result.enabled and self.source_track.samples:
            sample = self.source_track.samples[-1]
            self.origin = self.calibration_result.transform.apply_position(sample.position)


class ExternalCameraView(gl.GLViewWidget):
    def __init__(self):
        super().__init__()
        self.setBackgroundColor("#101418")
        self.setCameraPosition(distance=3.5, elevation=24, azimuth=42)

        grid = gl.GLGridItem()
        grid.setSize(4.0, 4.0, 1.0)
        grid.setSpacing(0.25, 0.25, 0.25)
        grid.setColor((60, 70, 80, 90))
        self.addItem(grid)

        self.add_axes()

        self.arkit_line = gl.GLLinePlotItem(width=3.0, antialias=True)
        self.sensor_line = gl.GLLinePlotItem(width=3.0, antialias=True)
        self.arkit_marker = gl.GLScatterPlotItem(size=9, pxMode=True)
        self.sensor_marker = gl.GLScatterPlotItem(size=9, pxMode=True)
        self.arkit_frustum = gl.GLLinePlotItem(width=2.0, antialias=True)
        self.sensor_frustum = gl.GLLinePlotItem(width=2.0, antialias=True)

        for item in [
            self.arkit_line,
            self.sensor_line,
            self.arkit_marker,
            self.sensor_marker,
            self.arkit_frustum,
            self.sensor_frustum,
        ]:
            self.addItem(item)

    def add_axes(self):
        axes = [
            (np.array([[0, 0, 0], [0.6, 0, 0]], dtype=float), (1.0, 0.2, 0.2, 1.0)),
            (np.array([[0, 0, 0], [0, 0.6, 0]], dtype=float), (0.2, 1.0, 0.2, 1.0)),
            (np.array([[0, 0, 0], [0, 0, 0.6]], dtype=float), (0.2, 0.4, 1.0, 1.0)),
        ]
        for points, color in axes:
            self.addItem(gl.GLLinePlotItem(pos=points, color=color, width=2.0, antialias=True))

    def update_scene(self, arkit_positions, sensor_positions, arkit_pose, sensor_pose):
        if len(arkit_positions) > 1:
            self.arkit_line.setData(pos=arkit_positions, color=(0.0, 0.85, 1.0, 1.0))
            self.arkit_marker.setData(pos=arkit_positions[-1:], color=(0.0, 0.85, 1.0, 1.0))

        if len(sensor_positions) > 1:
            self.sensor_line.setData(pos=sensor_positions, color=(1.0, 0.72, 0.18, 1.0))
            self.sensor_marker.setData(pos=sensor_positions[-1:], color=(1.0, 0.72, 0.18, 1.0))

        if arkit_pose is not None:
            self.arkit_frustum.setData(
                pos=camera_frustum_points(arkit_pose),
                color=(0.0, 0.85, 1.0, 0.9),
            )

        if sensor_pose is not None:
            self.sensor_frustum.setData(
                pos=camera_frustum_points(sensor_pose),
                color=(1.0, 0.72, 0.18, 0.9),
            )


def camera_frustum_points(sample, scale=0.18):
    rotation = quaternion_to_matrix(sample.quaternion)
    origin = sample.position
    forward = rotation @ np.array([0.0, 1.0, 0.0], dtype=float)
    right = rotation @ np.array([1.0, 0.0, 0.0], dtype=float)
    up = rotation @ np.array([0.0, 0.0, 1.0], dtype=float)

    center = origin + forward * scale
    corners = [
        center + right * scale * 0.55 + up * scale * 0.35,
        center - right * scale * 0.55 + up * scale * 0.35,
        center - right * scale * 0.55 - up * scale * 0.35,
        center + right * scale * 0.55 - up * scale * 0.35,
    ]

    segments = []
    for corner in corners:
        segments.extend([origin, corner])
    for index in range(4):
        segments.extend([corners[index], corners[(index + 1) % 4]])
    segments.extend([origin, origin + forward * scale * 1.35])
    return np.array(segments, dtype=float)


class StatsPanel(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout()
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.arkit_label = QLabel("ARKit: waiting")
        self.sensor_label = QLabel("Sensor: waiting")
        self.error_label = QLabel("Error: waiting for paired samples")
        self.timing_label = QLabel("Timing: waiting for calibration")
        self.calibration_label = QLabel("Calibration: waiting for motion")
        self.legend_label = QLabel("ARKit = cyan | Sensor = amber")

        for label in [
            self.arkit_label,
            self.sensor_label,
            self.error_label,
            self.timing_label,
            self.calibration_label,
            self.legend_label,
        ]:
            label.setWordWrap(True)
            layout.addWidget(label)

        self.setLayout(layout)

    def update_stream(self, stream, stats):
        text = (
            f"{stream}: {stats['fps']:.1f} fps | packets {stats['packets']} | v{stats.get('protocol_version', 1)} | "
            f"drops {stats['drops']} | latency {stats['latency_ms']:.1f} ms"
        )
        if stream == "arkit":
            self.arkit_label.setText(text)
        else:
            self.sensor_label.setText(text)

    def update_error(self, position_error, angle_error, time_delta):
        if position_error is None:
            self.error_label.setText("Error: waiting for paired samples")
            return

        self.error_label.setText(
            f"Error: position {position_error:.3f} m | angle {angle_error:.2f} deg | "
            f"time delta {time_delta * 1000.0:.1f} ms"
        )

    def update_calibration(self, result, apply_calibration):
        if not result.enabled:
            self.timing_label.setText("Timing: waiting for calibration")
            self.calibration_label.setText("Calibration: waiting for enough paired motion")
            return

        mode = "applied" if apply_calibration else "estimated"
        angle_text = "--" if result.angle_rmse is None else f"{result.angle_rmse:.2f} deg"
        position_text = "--" if result.position_rmse is None else f"{result.position_rmse:.3f} m"
        mean_time_text = "--" if result.mean_time_error is None else f"{result.mean_time_error * 1000.0:+.1f} ms"
        time_rmse_text = "--" if result.time_rmse is None else f"{result.time_rmse * 1000.0:.1f} ms"
        max_time_text = "--" if result.max_time_error is None else f"{result.max_time_error * 1000.0:.1f} ms"

        self.timing_label.setText(
            f"Timing: offset {result.time_offset * 1000.0:+.0f} ms | "
            f"mean residual {mean_time_text} | rmse {time_rmse_text} | max {max_time_text}"
        )
        self.calibration_label.setText(
            f"Calibration {mode}: {result.quality} | dt {result.time_offset * 1000.0:+.0f} ms | "
            f"slope {result.time_slope:.8f} | "
            f"scale {result.scale:.4f} | pos rmse {position_text} | "
            f"angle rmse {angle_text} | pairs {result.pair_count} | "
            f"inliers {result.inlier_ratio:.0%} | motion {result.motion_coverage}"
        )


class MainWindow(QMainWindow):
    def __init__(self, host, arkit_port, sensor_port, pairing_window, max_time_offset, arkit_csv=None, sensor_csv=None):
        super().__init__()
        self.host = host
        self.arkit_port = arkit_port
        self.sensor_port = sensor_port
        self.pairing_window = pairing_window
        self.max_time_offset = max_time_offset
        self.receivers = []
        self.tracks = {"arkit": PoseTrack(), "sensor": PoseTrack()}
        self.calibrator = AdaptiveCalibrator(max_time_offset=max_time_offset, pairing_window=pairing_window)
        self.calibrated_sensor_track = CalibratedSensorTrack(self.tracks["sensor"], self.calibrator.result)
        self.apply_calibration = False
        self.show_all = False
        self.last_calibration_update = 0.0
        self.calibration_file = Path("calibration.json")
        self.arkit_csv = arkit_csv
        self.sensor_csv = sensor_csv

        self.init_ui()
        self.apply_stylesheet()
        self.load_offline_inputs_if_needed()

        self.render_timer = QTimer()
        self.render_timer.timeout.connect(self.update_render)
        self.render_timer.start(33)

    def load_offline_inputs_if_needed(self):
        if not self.arkit_csv or not self.sensor_csv:
            return
        for sample in load_pose_csv(self.arkit_csv, "arkit"):
            self.tracks["arkit"].append(sample)
        for sample in load_pose_csv(self.sensor_csv, "sensor"):
            self.tracks["sensor"].append(sample)

        result = self.calibrator.update(self.tracks["arkit"].samples, self.tracks["sensor"].samples)
        self.calibrated_sensor_track.calibration_result = result
        self.stats_panel.update_stream("arkit", {
            "fps": 0.0,
            "packets": len(self.tracks["arkit"].samples),
            "drops": 0,
            "latency_ms": 0.0,
            "protocol_version": self.tracks["arkit"].samples[-1].protocol_version if self.tracks["arkit"].samples else 1,
        })
        self.stats_panel.update_stream("sensor", {
            "fps": 0.0,
            "packets": len(self.tracks["sensor"].samples),
            "drops": 0,
            "latency_ms": 0.0,
            "protocol_version": self.tracks["sensor"].samples[-1].protocol_version if self.tracks["sensor"].samples else 1,
        })
        self.stats_panel.update_calibration(result, self.apply_calibration)

    def init_ui(self):
        self.setWindowTitle("ARPose Tracking Validator")
        self.resize(1280, 820)

        root = QWidget()
        root_layout = QHBoxLayout()
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        side = QWidget()
        side.setFixedWidth(320)
        side_layout = QVBoxLayout()
        side_layout.setContentsMargins(14, 14, 14, 14)
        side_layout.setSpacing(14)

        service_box = QGroupBox("Streams")
        service_layout = QVBoxLayout()

        self.load_arkit_csv_button = QPushButton("Load iPhone CSV")
        self.load_sensor_csv_button = QPushButton("Load Robot Arm CSV")
        self.arkit_csv_label = QLabel("iPhone: Not loaded")
        self.sensor_csv_label = QLabel("Robot Arm: Not loaded")
        self.clear_data_button = QPushButton("Clear All Data")

        self.start_button = QPushButton("Start Live Validation")
        self.stop_button = QPushButton("Stop")
        self.reset_button = QPushButton("Reset Origins")
        self.save_calibration_button = QPushButton("Save Calibration")
        self.load_calibration_button = QPushButton("Load Calibration")
        self.show_all_checkbox = QCheckBox("Show all history")
        self.apply_calibration_checkbox = QCheckBox("Apply adaptive calibration")

        self.load_arkit_csv_button.clicked.connect(self.load_arkit_csv)
        self.load_sensor_csv_button.clicked.connect(self.load_sensor_csv)
        self.clear_data_button.clicked.connect(self.clear_all_data)
        self.start_button.clicked.connect(self.start_receivers)
        self.stop_button.clicked.connect(self.stop_receivers)
        self.reset_button.clicked.connect(self.reset_origins)
        self.save_calibration_button.clicked.connect(self.save_calibration)
        self.load_calibration_button.clicked.connect(self.load_calibration)
        self.show_all_checkbox.stateChanged.connect(self.change_history_mode)
        self.apply_calibration_checkbox.stateChanged.connect(self.change_calibration_mode)

        self.arkit_csv_label.setWordWrap(True)
        self.sensor_csv_label.setWordWrap(True)
        self.arkit_csv_label.setStyleSheet("font-size: 11px; color: #a0a8b0;")
        self.sensor_csv_label.setStyleSheet("font-size: 11px; color: #a0a8b0;")

        for widget in [
            self.load_arkit_csv_button,
            self.arkit_csv_label,
            self.load_sensor_csv_button,
            self.sensor_csv_label,
            self.clear_data_button,
            self.start_button,
            self.stop_button,
            self.reset_button,
            self.save_calibration_button,
            self.load_calibration_button,
            self.show_all_checkbox,
            self.apply_calibration_checkbox,
        ]:
            service_layout.addWidget(widget)

        self.endpoint_label = QLabel(
            f"Bind host: {self.host}\nARKit UDP: {self.arkit_port}\nSensor UDP: {self.sensor_port}"
        )
        self.endpoint_label.setWordWrap(True)
        service_layout.addWidget(self.endpoint_label)
        service_box.setLayout(service_layout)

        self.stats_panel = StatsPanel()

        side_layout.addWidget(service_box)
        side_layout.addWidget(self.stats_panel)
        side_layout.addStretch()
        side.setLayout(side_layout)

        self.view = ExternalCameraView()
        root_layout.addWidget(side)
        root_layout.addWidget(self.view, 1)
        root.setLayout(root_layout)
        self.setCentralWidget(root)

    def apply_stylesheet(self):
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background-color: #151b1f;
                color: #edf2f4;
                font-size: 13px;
            }
            QGroupBox {
                border: 1px solid #34434c;
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 12px;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }
            QPushButton {
                background-color: #26343c;
                border: 1px solid #41535f;
                border-radius: 7px;
                padding: 9px 12px;
            }
            QPushButton:hover {
                background-color: #31444f;
            }
            QCheckBox {
                padding: 4px;
            }
            QLabel {
                color: #edf2f4;
            }
            """
        )

    def start_receivers(self):
        if self.receivers:
            return
        for stream, port in [("arkit", self.arkit_port), ("sensor", self.sensor_port)]:
            receiver = PoseReceiverThread(stream, port, self.host)
            receiver.sample_received.connect(self.on_sample)
            receiver.status_updated.connect(self.stats_panel.update_stream)
            receiver.error_occurred.connect(self.on_error)
            receiver.start()
            self.receivers.append(receiver)

    def stop_receivers(self):
        for receiver in self.receivers:
            receiver.stop()
            receiver.wait()
        self.receivers = []

    def reset_origins(self):
        for track in self.tracks.values():
            track.reset_origin()
        self.calibrated_sensor_track.reset_origin()

    def change_history_mode(self, state):
        self.show_all = state == Qt.CheckState.Checked.value

    def change_calibration_mode(self, state):
        self.apply_calibration = state == Qt.CheckState.Checked.value
        self.calibrated_sensor_track.reset_origin()
        self.stats_panel.update_calibration(self.calibrator.result, self.apply_calibration)

    def save_calibration(self):
        if not self.calibrator.result.enabled:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Calibration", str(self.calibration_file), "JSON Files (*.json)")
        if not path:
            return
        payload = self.calibrator.result.to_dict()
        payload["saved_at"] = time.time()
        payload["format"] = "arpose_calibration_v1"
        Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        self.calibration_file = Path(path)

    def load_calibration(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Calibration", str(self.calibration_file), "JSON Files (*.json)")
        if not path:
            return
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        result = CalibrationResult.from_dict(data)
        result.enabled = True
        result.quality = data.get("quality", "loaded")
        self.calibrator.result = result
        self.calibrated_sensor_track.calibration_result = result
        self.calibrated_sensor_track.reset_origin()
        self.calibration_file = Path(path)
        self.stats_panel.update_calibration(result, self.apply_calibration)

    def on_sample(self, sample):
        self.tracks[sample.stream].append(sample)
        self.update_calibration_if_needed()
        self.update_error_metrics(sample)

    def on_error(self, stream, message):
        print(f"[{stream}] {message}")

    def update_error_metrics(self, sample):
        paired = self.current_error_pair(sample)
        if paired is None:
            self.stats_panel.update_error(None, None, None)
            return

        arkit, sensor, delta = paired
        arkit_origin = self.tracks["arkit"].origin
        if arkit_origin is None:
            arkit_origin = np.zeros(3, dtype=float)
        arkit_pos = arkit.position - arkit_origin

        if self.apply_calibration and self.calibrator.result.enabled:
            sensor_position = self.calibrator.result.transform.apply_position(sensor.position)
            sensor_pos = sensor_position - arkit_origin
            sensor_quaternion = self.calibrator.result.transform.apply_quaternion(sensor.quaternion)
        else:
            sensor_origin = self.tracks["sensor"].origin
            if sensor_origin is None:
                sensor_origin = np.zeros(3, dtype=float)
            sensor_pos = sensor.position - sensor_origin
            sensor_quaternion = sensor.quaternion

        position_error = float(np.linalg.norm(arkit_pos - sensor_pos))
        angle_error = quaternion_angle_error_degrees(arkit.quaternion, sensor_quaternion)
        self.stats_panel.update_error(position_error, angle_error, delta)

    def current_error_pair(self, sample):
        if self.apply_calibration and self.calibrator.result.enabled:
            target_stream = "sensor" if sample.stream == "arkit" else "arkit"
            if sample.stream == "arkit":
                target_time = self.calibrator.result.arkit_to_sensor_time(sample.sender_time)
                other, delta = nearest_by_sender_time(
                    self.tracks[target_stream].samples,
                    target_time,
                    self.pairing_window,
                )
                if other is None:
                    return None
                return sample, other, delta

            sample_time = self.calibrator.sample_time(sample)
            target_time = self.calibrator.result.sensor_to_arkit_time(sample_time)
            other, delta = nearest_by_sender_time(
                self.tracks[target_stream].samples,
                target_time,
                self.pairing_window,
            )
            if other is None:
                return None
            return other, sample, delta

        other_stream = "sensor" if sample.stream == "arkit" else "arkit"
        other, delta = nearest_by_sender_time(
            self.tracks[other_stream].samples,
            sample.sender_time,
            self.pairing_window,
        )
        if other is None:
            return None

        arkit = sample if sample.stream == "arkit" else other
        sensor = other if sample.stream == "arkit" else sample
        return arkit, sensor, delta

    def update_calibration_if_needed(self):
        now = time.monotonic()
        if now - self.last_calibration_update < 0.5:
            return
        self.last_calibration_update = now
        result = self.calibrator.update(self.tracks["arkit"].samples, self.tracks["sensor"].samples)
        self.calibrated_sensor_track.calibration_result = result
        self.stats_panel.update_calibration(result, self.apply_calibration)

    def update_render(self):
        window = None if self.show_all else 5.0
        arkit_positions = self.tracks["arkit"].positions(window)
        arkit_pose = self.tracks["arkit"].latest_relative()

        # Always show both tracks, even when calibration is applied
        if self.apply_calibration and self.calibrated_sensor_track is not None:
            sensor_track = self.calibrated_sensor_track
            arkit_origin = self.tracks["arkit"].origin
            sensor_positions = sensor_track.positions(window, reference_origin=arkit_origin)
            sensor_pose = sensor_track.latest_relative(reference_origin=arkit_origin)
        else:
            sensor_track = self.tracks["sensor"]
            sensor_positions = sensor_track.positions(window)
            sensor_pose = sensor_track.latest_relative()

            # Only apply manual alignment when calibration is NOT applied
            if len(arkit_positions) > 0 and len(sensor_positions) > 0:
                sensor_positions = self.align_trajectories(arkit_positions, sensor_positions)

        # Align first frame for better visualization (both calibrated and uncalibrated modes)
        if len(arkit_positions) > 0 and len(sensor_positions) > 0:
            offset = arkit_positions[0] - sensor_positions[0]
            sensor_positions = sensor_positions + offset

        # Debug: print lengths
        if len(sensor_positions) == 0:
            print(f"Warning: sensor_positions is empty! apply_calibration={self.apply_calibration}, calibrated_track exists={self.calibrated_sensor_track is not None}")

        self.view.update_scene(arkit_positions, sensor_positions, arkit_pose, sensor_pose)

    def align_trajectories(self, arkit_pos, sensor_pos, align_frames=100):
        """Align sensor trajectory to arkit using first N frames to eliminate coordinate system differences"""
        if len(arkit_pos) < 2 or len(sensor_pos) < 2:
            return sensor_pos

        # Use first N frames for alignment
        n = min(align_frames, len(arkit_pos), len(sensor_pos))
        arkit_subset = arkit_pos[:n]
        sensor_subset = sensor_pos[:n]

        # Find motion start (skip static initial frames)
        motion_threshold = 0.001  # 1mm
        arkit_start = 0
        for i in range(1, len(arkit_subset)):
            if np.linalg.norm(arkit_subset[i] - arkit_subset[0]) > motion_threshold:
                arkit_start = i
                break

        sensor_start = 0
        for i in range(1, len(sensor_subset)):
            if np.linalg.norm(sensor_subset[i] - sensor_subset[0]) > motion_threshold:
                sensor_start = i
                break

        # Align motion start points
        arkit_motion = arkit_subset[arkit_start:]
        sensor_motion = sensor_subset[sensor_start:]

        if len(arkit_motion) < 10 or len(sensor_motion) < 10:
            # Not enough motion data, use simple first frame alignment
            offset = arkit_pos[0] - sensor_pos[0]
            return sensor_pos + offset

        # Compute centroids
        arkit_centroid = np.mean(arkit_motion, axis=0)
        sensor_centroid = np.mean(sensor_motion, axis=0)

        # Center the point sets
        arkit_centered = arkit_motion - arkit_centroid
        sensor_centered = sensor_motion - sensor_centroid

        # Compute optimal rotation using SVD (Kabsch algorithm)
        n_pairs = min(len(arkit_centered), len(sensor_centered))
        H = sensor_centered[:n_pairs].T @ arkit_centered[:n_pairs]
        U, _, Vt = np.linalg.svd(H)
        R = Vt.T @ U.T

        # Ensure proper rotation (det(R) = 1)
        if np.linalg.det(R) < 0:
            Vt[-1, :] *= -1
            R = Vt.T @ U.T

        # Compute translation
        t = arkit_centroid - R @ sensor_centroid

        # Apply transformation to all sensor positions
        sensor_aligned = (R @ sensor_pos.T).T + t

        return sensor_aligned

    def load_arkit_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load iPhone Pose CSV",
            str(Path.home()),
            "CSV Files (*.csv);;All Files (*.*)"
        )
        if not path:
            return

        self.stop_receivers()
        self.tracks["arkit"] = PoseTrack()

        try:
            samples = load_pose_csv(path, "arkit")
            for sample in samples:
                self.tracks["arkit"].append(sample)

            self.arkit_csv = path
            filename = Path(path).name
            self.arkit_csv_label.setText(f"iPhone: {filename} ({len(samples)} samples)")
            self.stats_panel.update_stream("arkit", {
                "fps": 0.0,
                "packets": len(samples),
                "drops": 0,
                "latency_ms": 0.0,
                "protocol_version": samples[-1].protocol_version if samples else 1,
            })

            if self.sensor_csv:
                self.update_calibration_from_loaded_data()

        except Exception as e:
            self.arkit_csv_label.setText(f"iPhone: Error loading file")
            print(f"Error loading ARKit CSV: {e}")

    def load_sensor_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Robot Arm Pose CSV",
            str(Path.home()),
            "CSV Files (*.csv);;All Files (*.*)"
        )
        if not path:
            return

        self.stop_receivers()
        self.tracks["sensor"] = PoseTrack()

        try:
            samples = load_pose_csv(path, "sensor")
            for sample in samples:
                self.tracks["sensor"].append(sample)

            self.sensor_csv = path
            filename = Path(path).name
            self.sensor_csv_label.setText(f"Robot Arm: {filename} ({len(samples)} samples)")
            self.stats_panel.update_stream("sensor", {
                "fps": 0.0,
                "packets": len(samples),
                "drops": 0,
                "latency_ms": 0.0,
                "protocol_version": samples[-1].protocol_version if samples else 1,
            })

            if self.arkit_csv:
                self.update_calibration_from_loaded_data()

            # Update calibrated_sensor_track to reference the new sensor track
            self.calibrated_sensor_track.source_track = self.tracks["sensor"]

        except Exception as e:
            self.sensor_csv_label.setText(f"Robot Arm: Error loading file")
            print(f"Error loading sensor CSV: {e}")

    def update_calibration_from_loaded_data(self):
        if not self.tracks["arkit"].samples or not self.tracks["sensor"].samples:
            return

        result = self.calibrator.update(self.tracks["arkit"].samples, self.tracks["sensor"].samples)
        self.calibrated_sensor_track.calibration_result = result
        self.stats_panel.update_calibration(result, self.apply_calibration)

    def clear_all_data(self):
        self.stop_receivers()
        self.tracks["arkit"] = PoseTrack()
        self.tracks["sensor"] = PoseTrack()
        self.calibrator = AdaptiveCalibrator(max_time_offset=self.max_time_offset, pairing_window=self.pairing_window)
        self.calibrated_sensor_track = CalibratedSensorTrack(self.tracks["sensor"], self.calibrator.result)
        self.arkit_csv = None
        self.sensor_csv = None
        self.arkit_csv_label.setText("iPhone: Not loaded")
        self.sensor_csv_label.setText("Robot Arm: Not loaded")
        self.stats_panel.update_stream("arkit", {
            "fps": 0.0,
            "packets": 0,
            "drops": 0,
            "latency_ms": 0.0,
            "protocol_version": 1,
        })
        self.stats_panel.update_stream("sensor", {
            "fps": 0.0,
            "packets": 0,
            "drops": 0,
            "latency_ms": 0.0,
            "protocol_version": 1,
        })
        self.stats_panel.update_error(None, None, None)
        self.stats_panel.update_calibration(self.calibrator.result, self.apply_calibration)

    def closeEvent(self, event):
        self.stop_receivers()
        event.accept()


def main():
    parser = argparse.ArgumentParser(description="Validate ARKit tracking against a wired sensor stream.")
    parser.add_argument("--host", default="0.0.0.0", help="Host/IP to bind UDP sockets to.")
    parser.add_argument("--arkit-port", type=int, default=5555, help="ARKit UDP pose port.")
    parser.add_argument("--sensor-port", type=int, default=5556, help="Wired sensor UDP pose port.")
    parser.add_argument(
        "--pairing-window",
        type=float,
        default=0.05,
        help="Maximum sender timestamp delta for error pairing, in seconds.",
    )
    parser.add_argument(
        "--max-time-offset",
        type=float,
        default=5.0,
        help="Maximum sensor time offset to scan during adaptive calibration, in seconds.",
    )
    parser.add_argument("--arkit-csv", default=None, help="Offline ARKit pose CSV path.")
    parser.add_argument("--sensor-csv", default=None, help="Offline wired sensor pose CSV path.")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    window = MainWindow(
        args.host,
        args.arkit_port,
        args.sensor_port,
        args.pairing_window,
        args.max_time_offset,
        arkit_csv=args.arkit_csv,
        sensor_csv=args.sensor_csv,
    )
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
