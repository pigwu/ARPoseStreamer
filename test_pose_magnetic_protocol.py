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
            with (output / "magnetic.csv").open(newline="", encoding="utf-8") as handle:
                magnetic_rows = list(csv.reader(handle))

            self.assertEqual(len(pose_rows), 2)
            self.assertEqual(len(magnetic_rows), 1)


if __name__ == "__main__":
    unittest.main()
