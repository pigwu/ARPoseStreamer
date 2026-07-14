import unittest

from udp_video_debug_ui import LatencyClockCompensator


class LatencyClockCompensatorTests(unittest.TestCase):
    def test_pose_reference_removes_wall_clock_offset(self) -> None:
        clock = LatencyClockCompensator()

        pose_latency = clock.observe(
            sender_timestamp=1_000.0,
            receive_wall_time=1_000.850,
            receive_monotonic=10.0,
            is_pose_reference=True,
        )

        self.assertAlmostEqual(pose_latency or 0.0, 0.0, places=6)
        self.assertAlmostEqual(clock.offset_seconds or 0.0, 0.850, places=6)
        self.assertAlmostEqual(clock.compensate_raw_delay(0.872) or 0.0, 22.0, places=6)
        self.assertEqual(clock.reference_name, "Pose packets")

    def test_pose_reference_replaces_video_fallback(self) -> None:
        clock = LatencyClockCompensator()
        clock.observe(
            sender_timestamp=1_000.0,
            receive_wall_time=1_000.870,
            receive_monotonic=10.0,
            is_pose_reference=False,
        )
        self.assertAlmostEqual(clock.offset_seconds or 0.0, 0.870, places=6)

        clock.observe(
            sender_timestamp=1_001.0,
            receive_wall_time=1_001.850,
            receive_monotonic=11.0,
            is_pose_reference=True,
        )

        self.assertAlmostEqual(clock.offset_seconds or 0.0, 0.850, places=6)
        self.assertEqual(clock.reference_name, "Pose packets")

    def test_invalid_timestamp_is_ignored(self) -> None:
        clock = LatencyClockCompensator()

        latency = clock.observe(
            sender_timestamp=0.0,
            receive_wall_time=100_000.0,
            receive_monotonic=10.0,
            is_pose_reference=True,
        )

        self.assertIsNone(latency)
        self.assertIsNone(clock.offset_seconds)


if __name__ == "__main__":
    unittest.main()
