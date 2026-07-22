import json
import struct
import unittest

import numpy as np

from anyskin_serial_mapper import (
    ASKN_MAGIC,
    ASKN_PACKET,
    SerialStreamDecoder,
    compute_response_scores,
    mapping_confidence,
    parse_chip_line,
    parse_serial_line,
)


class SerialParserTests(unittest.TestCase):
    def test_parse_twenty_csv_values(self):
        values = parse_serial_line(",".join(str(value) for value in range(20)))
        self.assertEqual(values.shape, (5, 4))
        self.assertEqual(values[4, 3], 19)

    def test_parse_xyz_only(self):
        values = parse_serial_line(" ".join(str(value) for value in range(15)))
        self.assertTrue(np.isnan(values[:, 0]).all())
        np.testing.assert_array_equal(values[:, 1:].reshape(-1), np.arange(15))

    def test_parse_prefixed_board_line(self):
        text = "ASKN,42,123456," + ",".join(str(value) for value in range(20))
        values = parse_serial_line(text)
        self.assertEqual(values[0, 0], 0)
        self.assertEqual(values[4, 3], 19)

    def test_parse_json_sensor_objects(self):
        payload = {f"S{i}": {"x": i + 0.1, "y": i + 0.2, "z": i + 0.3} for i in range(5)}
        values = parse_serial_line(json.dumps(payload))
        self.assertAlmostEqual(float(values[3, 2]), 3.2, places=5)

    def test_fragmented_binary_packet(self):
        packet = ASKN_PACKET.pack(ASKN_MAGIC, 7, 9000, *[float(i) for i in range(20)])
        decoder = SerialStreamDecoder("auto")
        self.assertEqual(decoder.feed(packet[:31]), [])
        output = decoder.feed(packet[31:])
        self.assertEqual(len(output), 1)
        kind, frame = output[0]
        self.assertEqual(kind, "frame")
        self.assertEqual(frame["sequence"], 7)
        self.assertEqual(frame["values"][4, 3], 19)

    def test_binary_packet_with_newline_byte_is_not_mistaken_for_text(self):
        # Sequence 10 makes byte 0x0a appear immediately after the magic.
        packet = ASKN_PACKET.pack(ASKN_MAGIC, 10, 9000, *[float(i) for i in range(20)])
        output = SerialStreamDecoder("auto").feed(packet)
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0][0], "frame")
        self.assertEqual(output[0][1]["sequence"], 10)

    def test_text_stream_can_hold_multiple_lines(self):
        line = ",".join(str(value) for value in range(20)).encode() + b"\n"
        output = SerialStreamDecoder("auto").feed(line + line)
        self.assertEqual([kind for kind, _ in output], ["frame", "frame"])

    def test_parse_per_chip_firmware_line(self):
        chip_id, values = parse_chip_line(
            "chip 3 addr=0x14 status=0xFF t=24.31 x=-1129.50 y=-1166.25 z=735.44"
        )
        self.assertEqual(chip_id, 3)
        np.testing.assert_allclose(values, [24.31, -1129.50, -1166.25, 735.44], rtol=1e-5)

    def test_five_chip_lines_are_assembled_into_one_frame(self):
        decoder = SerialStreamDecoder("auto")
        payload = [b"------------------------\r\n"]
        for chip_id in (3, 1, 4, 0, 2):
            payload.append(
                f"chip {chip_id} addr=0x0E status=0x00 t={24 + chip_id} "
                f"x={100 + chip_id} y={200 + chip_id} z={300 + chip_id}\r\n".encode()
            )
        output = decoder.feed(b"".join(payload))
        frames = [frame for kind, frame in output if kind == "frame"]
        errors = [error for kind, error in output if kind == "error"]
        self.assertEqual(errors, [])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["channel_ids"], [0, 1, 2, 3, 4])
        self.assertEqual(frames[0]["values"][3, 1], 103)


class MappingTests(unittest.TestCase):
    def test_response_identifies_changed_channel(self):
        baseline = np.zeros((5, 4), dtype=np.float32)
        samples = np.zeros((20, 5, 4), dtype=np.float32)
        samples[:, 3, 1:4] = (10, 2, 1)
        samples[:, 1, 1] = 2
        scores = compute_response_scores(samples, baseline)
        channel, confidence = mapping_confidence(scores)
        self.assertEqual(channel, 3)
        self.assertGreater(confidence, 0.7)


if __name__ == "__main__":
    unittest.main()
