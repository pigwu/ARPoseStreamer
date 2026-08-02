import csv
import tempfile
import time
import unittest
import uuid
import zlib
from pathlib import Path

import anyskin_hotspot_sender as board
import pose_magnetic_receiver as receiver


class PoseMagneticProtocolTests(unittest.TestCase):
    def build_pose_only_packet(self) -> bytes:
        session_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        payload = receiver.HEADER_STRUCT.pack(
            receiver.MAGIC,
            receiver.PROTOCOL_VERSION,
            receiver.POSE_PRESENT_FLAG,
            1,
            session_id.bytes,
            1_700_000_000.0,
            7,
            1_700_000_000.0,
            123.0,
            1.0,
            2.0,
            3.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0,
            0,
        )
        return payload + receiver.CRC_STRUCT.pack(
            zlib.crc32(payload) & receiver.UINT32_MASK
        )

    def test_anyskin_board_packet_matches_askn_v1(self) -> None:
        packet = board.build_packet(sequence=42, mcu_time_us=123_456, elapsed=0.25)
        self.assertEqual(len(packet), 96)
        self.assertEqual(packet[:4], bytes((0x4E, 0x4B, 0x53, 0x41)))
        decoded = board.ASKN_PACKET.unpack(packet)
        self.assertEqual(decoded[:3], (board.ASKN_MAGIC, 42, 123_456))
        self.assertEqual(len(decoded[3:]), 20)

    def test_apm1_full_packet_and_crc_rejection(self) -> None:
        packet, session_id = receiver._build_self_test_packet()
        decoded = receiver.decode_apm1_packet(packet)
        self.assertEqual(decoded.session_id, session_id)
        self.assertIsNotNone(decoded.pose)
        self.assertEqual(len(decoded.magnetic_samples), 2)

        corrupted = bytearray(packet)
        corrupted[receiver.HEADER_STRUCT.size + 3] ^= 0x40
        with self.assertRaises(receiver.APM1CRCError):
            receiver.decode_apm1_packet(corrupted)

    def test_apm2_decodes_right_and_left_and_tracks_sequences_independently(self) -> None:
        packet, _ = receiver._build_apm2_self_test_packet()
        decoded = receiver.decode_apm_packet(packet)

        self.assertEqual(
            [sample.side for sample in decoded.magnetic_samples],
            [receiver.RIGHT_BOARD, receiver.LEFT_BOARD],
        )
        self.assertEqual(
            [sample.sequence for sample in decoded.magnetic_samples],
            [25, 25],
        )

        stats = receiver.ReceiverStats()
        stats.observe(decoded, ("127.0.0.1", 5558), time.time(), time.monotonic())
        self.assertEqual(len(stats.magnetic_trackers), 2)
        self.assertTrue(all(tracker.missing == 0 for tracker in stats.magnetic_trackers.values()))

    def test_apm1_magnetic_samples_default_to_right_board(self) -> None:
        packet, _ = receiver._build_self_test_packet()
        decoded = receiver.decode_apm_packet(packet)
        self.assertTrue(decoded.magnetic_samples)
        self.assertTrue(
            all(sample.side == receiver.RIGHT_BOARD for sample in decoded.magnetic_samples)
        )

    def test_pose_only_packet_is_valid_when_sensor_is_absent(self) -> None:
        decoded = receiver.decode_apm1_packet(self.build_pose_only_packet())
        self.assertIsNotNone(decoded.pose)
        self.assertEqual(decoded.pose.sequence, 7)
        self.assertEqual(decoded.magnetic_samples, ())

    def test_pose_only_csv_has_no_fake_magnetic_row(self) -> None:
        decoded = receiver.decode_apm1_packet(self.build_pose_only_packet())
        with tempfile.TemporaryDirectory(prefix="apm1-pose-only-") as directory:
            output = Path(directory)
            with receiver.CSVRecorder(output) as recorder:
                recorder.write_packet(
                    decoded,
                    ("127.0.0.1", 12345),
                    receive_unix=time.time(),
                    receive_monotonic=time.monotonic(),
                )

            with (output / "pose.csv").open(newline="", encoding="utf-8") as handle:
                pose_rows = list(csv.reader(handle))
            with (output / "magnetic_right.csv").open(newline="", encoding="utf-8") as handle:
                right_rows = list(csv.reader(handle))
            with (output / "magnetic_left.csv").open(newline="", encoding="utf-8") as handle:
                left_rows = list(csv.reader(handle))

            self.assertEqual(len(pose_rows), 2)
            self.assertEqual(len(right_rows), 1)
            self.assertEqual(len(left_rows), 1)

    def test_dual_board_csv_rows_are_written_to_separate_files(self) -> None:
        packet, _ = receiver._build_apm2_self_test_packet()
        decoded = receiver.decode_apm_packet(packet)
        with tempfile.TemporaryDirectory(prefix="apm2-dual-board-") as directory:
            output = Path(directory)
            with receiver.CSVRecorder(output) as recorder:
                recorder.write_packet(
                    decoded,
                    ("127.0.0.1", 12345),
                    receive_unix=time.time(),
                    receive_monotonic=time.monotonic(),
                )

            for name in ("magnetic_right.csv", "magnetic_left.csv"):
                with (output / name).open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.reader(handle))
                self.assertEqual(len(rows), 2, name)
                self.assertEqual(rows[1][9], "25")


if __name__ == "__main__":
    unittest.main()
