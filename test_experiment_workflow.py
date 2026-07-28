import csv
import http.client
import json
import math
import socket
import tempfile
import threading
import unittest
from pathlib import Path

import zarr
from PyQt6.QtCore import Qt

from capture_upload_server import (
    UploadHandler,
    create_upload_server,
    migrate_uuid_experiment_directories,
    reconcile_upload_state,
    readable_experiment_name,
)
from experiment_data import ExperimentDataset, discover_experiments
from experiment_replay_ui import (
    CombinedReceiverThread,
    ReceiverDiagnosticsRecorder,
    _relative_magnetic_magnitudes,
    decode_remote_recording_ack,
    encode_remote_recording_command,
)
from experiment_zarr import AutoZarrExporter


class ExperimentWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.events: list[dict] = []
        self.server = create_upload_server(
            "127.0.0.1",
            0,
            self.root,
            self.events.append,
            auto_zarr=False,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def test_magnetic_magnitude_plot_is_relative_to_first_valid_sample(self) -> None:
        rows = [
            {"s0_x": "bad", "s0_y": 0, "s0_z": 0},
            {"s0_x": 3, "s0_y": 4, "s0_z": 0},
            {"s0_x": 0, "s0_y": 0, "s0_z": 10},
        ]

        values = _relative_magnetic_magnitudes(rows, 0)

        self.assertTrue(math.isnan(values[0]))
        self.assertEqual(values[1], 0.0)
        self.assertEqual(values[2], 5.0)

    def test_ultrawide_video_uses_a_stable_server_filename(self) -> None:
        self.assertEqual(
            UploadHandler.canonical_filename("ultrawide_video", "ARPoseStreamer-UltraWide.mp4"),
            "ultrawide_video.mp4",
        )

    def test_dataset_discovers_both_recorded_camera_files(self) -> None:
        folder = self.root / "dual-camera"
        folder.mkdir()
        (folder / "video.mp4").write_bytes(b"main")
        (folder / "ultrawide_video.mp4").write_bytes(b"ultrawide")
        (folder / "aruco_gripper.csv").write_text(
            "frame_index,experiment_time,status,offline_smoothed_mm\n"
            "0,0.12,tracking_gripper_distance,12.5\n",
            encoding="utf-8",
        )
        (folder / "aruco_gripper_state.json").write_text(
            json.dumps({"status": "complete", "detection_rate": 1.0}),
            encoding="utf-8",
        )
        (folder / "capture_manifest.json").write_text(
            json.dumps(
                {
                    "experimentID": "dual-camera",
                    "videoStartOffsetSeconds": 0.05,
                    "ultraWideVideoStartOffsetSeconds": 0.12,
                }
            ),
            encoding="utf-8",
        )
        (folder / "upload_state.json").write_text(
            json.dumps(
                {
                    "complete": True,
                    "components": {
                        "video": "video.mp4",
                        "ultrawide_video": "ultrawide_video.mp4",
                        "aruco_gripper": "aruco_gripper.csv",
                        "manifest": "capture_manifest.json",
                    },
                }
            ),
            encoding="utf-8",
        )

        dataset = ExperimentDataset.load(folder)

        self.assertEqual(dataset.video_path, folder / "video.mp4")
        self.assertEqual(dataset.ultrawide_video_path, folder / "ultrawide_video.mp4")
        self.assertEqual(dataset.gripper_path, folder / "aruco_gripper.csv")
        self.assertEqual(dataset.gripper.nearest(0.12)["offline_smoothed_mm"], "12.5")
        self.assertEqual(dataset.gripper_state["status"], "complete")
        self.assertAlmostEqual(dataset.video_start_offset_seconds, 0.05)
        self.assertAlmostEqual(dataset.ultrawide_video_start_offset_seconds, 0.12)

    def test_control_and_grouped_upload_create_one_experiment_folder(self) -> None:
        experiment_id = "12345678-1234-5678-9ABC-DEF012345678"
        self._post_json(
            "/experiment/control",
            {
                "event": "start",
                "experimentID": experiment_id,
                "eventUnixTime": 1_700_000_000.0,
                "eventMonotonicTime": 100.0,
            },
        )

        pose = b"sequence,sender_time,frame_time,relative_time,x,y,z,qx,qy,qz,qw\n1,1700000000.1,100.1,0.1,1,2,3,0,0,0,1\n"
        manifest = json.dumps(
            {
                "schemaVersion": 2,
                "experimentID": experiment_id,
                "experimentStartUnixTime": 1_700_000_000.0,
                "experimentStartMonotonicTime": 100.0,
                "durationSeconds": 1.0,
                "videoStartOffsetSeconds": 0.05,
            }
        ).encode()
        self._upload(experiment_id, "pose_csv", "pose.csv", pose, 1, 3)
        self._upload(
            experiment_id,
            "ultrawide_video",
            "ARPoseStreamer-UltraWide.mp4",
            b"ultrawide-video",
            2,
            3,
        )
        self._upload(experiment_id, "manifest", "capture_manifest.json", manifest, 3, 3)

        folder = self.root / readable_experiment_name(1_700_000_000.0)
        self.assertRegex(folder.name, r"^\d{8}-\d{6}$")
        self.assertTrue((folder / "pose.csv").is_file())
        self.assertEqual((folder / "ultrawide_video.mp4").read_bytes(), b"ultrawide-video")
        self.assertTrue((folder / "capture_manifest.json").is_file())
        state = json.loads((folder / "upload_state.json").read_text(encoding="utf-8"))
        self.assertTrue(state["complete"])
        self.assertEqual(state["components"]["pose_csv"], "pose.csv")
        self.assertEqual(state["components"]["ultrawide_video"], "ultrawide_video.mp4")
        self.assertTrue(any(event.get("event") == "start" for event in self.events))

        datasets = discover_experiments(self.root)
        self.assertEqual(len(datasets), 1)
        dataset = datasets[0]
        self.assertEqual(dataset.experiment_id, experiment_id)
        self.assertAlmostEqual(dataset.pose.times[0], 0.1)
        self.assertEqual(dataset.pose.nearest(0.1)["x"], "1")

    def test_remote_recording_protocol_round_trip(self) -> None:
        self.assertEqual(
            encode_remote_recording_command("request-42", "start"),
            b"PC_RECORD,1,request-42,START\n",
        )
        self.assertEqual(
            decode_remote_recording_ack(
                b"PC_RECORD_ACK,1,request-42,START,OK,recording\n"
            ),
            {
                "request_id": "request-42",
                "action": "START",
                "result": "OK",
                "state": "recording",
            },
        )

    def test_remote_recording_protocol_rejects_invalid_packets(self) -> None:
        with self.assertRaises(ValueError):
            encode_remote_recording_command("request,42", "START")
        with self.assertRaises(ValueError):
            encode_remote_recording_command("请求-42", "START")
        with self.assertRaises(ValueError):
            encode_remote_recording_command("request-42", "PAUSE")
        self.assertIsNone(decode_remote_recording_ack(b"APM1 binary payload"))
        self.assertIsNone(
            decode_remote_recording_ack(
                b"PC_RECORD_ACK,1,request-42,START,OK,unknown\n"
            )
        )

    def test_remote_recording_uses_a_dedicated_control_socket(self) -> None:
        phone_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        phone_socket.bind(("127.0.0.1", 0))
        phone_socket.settimeout(5.0)
        registration_port = int(phone_socket.getsockname()[1])

        port_reservation = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        port_reservation.bind(("127.0.0.1", 0))
        combined_port = int(port_reservation.getsockname()[1])
        port_reservation.close()

        acknowledgements: list[dict] = []
        acknowledgement_received = threading.Event()
        worker = CombinedReceiverThread(
            "127.0.0.1",
            combined_port,
            "127.0.0.1",
            registration_port,
            5560,
        )
        worker.recording_ack_ready.connect(
            lambda acknowledgement: (
                acknowledgements.append(acknowledgement),
                acknowledgement_received.set(),
            ),
            Qt.ConnectionType.DirectConnection,
        )

        try:
            worker.start()
            request_id = worker.request_recording_action("STATUS")
            command_source_port = None
            while command_source_port is None:
                datagram, address = phone_socket.recvfrom(4096)
                if datagram.startswith(b"PC_RECORD,"):
                    command_source_port = int(address[1])
                    phone_socket.sendto(
                        f"PC_RECORD_ACK,1,{request_id},STATUS,OK,idle\n".encode("ascii"),
                        address,
                    )

            self.assertNotEqual(command_source_port, combined_port)
            self.assertTrue(acknowledgement_received.wait(2.0))
            self.assertEqual(acknowledgements[0]["request_id"], request_id)
            self.assertEqual(acknowledgements[0]["state"], "idle")
        finally:
            worker.stop()
            worker.wait(2000)
            phone_socket.close()

    def test_legacy_magnetic_time_is_aligned_from_manifest(self) -> None:
        folder = self.root / "legacy"
        folder.mkdir()
        (folder / "capture_manifest.json").write_text(
            json.dumps(
                {
                    "createdAtUnixTime": 1_700_000_000.0,
                    "sessionStartFrameTime": 500.0,
                    "durationSeconds": 2.0,
                }
            ),
            encoding="utf-8",
        )
        with (folder / "magnetic.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sequence", "phone_monotonic_time", "s0_t", "s0_x", "s0_y", "s0_z"])
            writer.writerow([1, 500.25, 0, 1, 2, 3])

        dataset = ExperimentDataset.load(folder)
        self.assertAlmostEqual(dataset.magnetic.times[0], 0.25)

    def test_receiver_diagnostics_are_cut_to_control_timestamps(self) -> None:
        experiment_id = "diagnostics-session"
        folder = self.root / experiment_id
        recorder = ReceiverDiagnosticsRecorder(self.root)
        recorder.handle_control(
            {
                "event": "start",
                "experiment_id": experiment_id,
                "event_unix_time": 100.0,
                "directory": str(folder),
            }
        )
        recorder.record(
            {
                "kind": "video",
                "identifier": 1,
                "sender_time": 100.125,
                "pc_receive_time": 100.140,
                "pc_decode_time": 100.150,
                "corrected_latency_ms": 25.0,
            }
        )
        recorder.handle_control(
            {
                "event": "stop",
                "experiment_id": experiment_id,
                "event_unix_time": 100.2,
            }
        )

        with (folder / "receiver_transport.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(float(rows[0]["experiment_time"]), 0.125)

    def test_uuid_folder_is_migrated_to_readable_timestamp(self) -> None:
        experiment_id = "ABCDEF12-1234-5678-9ABC-DEF012345678"
        folder = self.root / experiment_id
        folder.mkdir()
        (folder / "experiment_state.json").write_text(
            json.dumps({"experiment_id": experiment_id, "start_unix_time": 1_700_000_100.0}),
            encoding="utf-8",
        )

        migrated = migrate_uuid_experiment_directories(self.root)

        expected = self.root / readable_experiment_name(1_700_000_100.0)
        self.assertEqual(migrated, [(folder, expected)])
        self.assertTrue(expected.is_dir())
        self.assertFalse(folder.exists())

    def test_split_uuid_folder_is_merged_into_readable_experiment(self) -> None:
        experiment_id = "CB5DC7CC-2D80-4947-825A-9ED0835F2FA4"
        start_unix = 1_700_000_200.0
        readable = self.root / readable_experiment_name(start_unix)
        readable.mkdir()
        (readable / "experiment_state.json").write_text(
            json.dumps({"experiment_id": experiment_id, "start_unix_time": start_unix}),
            encoding="utf-8",
        )
        (readable / "pose.csv").write_text("relative_time,x\n0.0,1\n", encoding="utf-8")
        (readable / "upload_state.json").write_text(
            json.dumps(
                {
                    "capture_id": experiment_id,
                    "expected_files": 3,
                    "components": {"pose_csv": "pose.csv"},
                    "uploaded_components": ["pose_csv"],
                    "complete": False,
                }
            ),
            encoding="utf-8",
        )

        split = self.root / experiment_id
        split.mkdir()
        (split / "experiment_state.json").write_text(
            json.dumps({"experiment_id": experiment_id, "stop_unix_time": start_unix + 2.0}),
            encoding="utf-8",
        )
        (split / "capture_manifest.json").write_text(
            json.dumps({"experimentID": experiment_id, "experimentStartUnixTime": start_unix}),
            encoding="utf-8",
        )
        (split / "video.mp4").write_bytes(b"video")
        (split / "upload_state.json").write_text(
            json.dumps(
                {
                    "capture_id": experiment_id,
                    "expected_files": 3,
                    "components": {"video": "video.mp4", "manifest": "capture_manifest.json"},
                    "uploaded_components": ["video", "manifest"],
                    "complete": False,
                }
            ),
            encoding="utf-8",
        )

        migrated = migrate_uuid_experiment_directories(self.root)

        self.assertEqual(migrated, [(split, readable)])
        self.assertFalse(split.exists())
        self.assertEqual((readable / "video.mp4").read_bytes(), b"video")
        experiment_state = json.loads((readable / "experiment_state.json").read_text(encoding="utf-8"))
        self.assertEqual(experiment_state["start_unix_time"], start_unix)
        self.assertEqual(experiment_state["stop_unix_time"], start_unix + 2.0)
        upload_state = json.loads((readable / "upload_state.json").read_text(encoding="utf-8"))
        self.assertTrue(upload_state["complete"])
        self.assertEqual(set(upload_state["components"]), {"pose_csv", "video", "manifest"})
        self.assertTrue((readable / "_merged_fragments" / experiment_id).is_dir())

    def test_out_of_order_stop_then_start_uses_one_readable_folder(self) -> None:
        experiment_id = "87654321-4321-8765-CBA9-876543210FED"
        start_unix = 1_700_000_300.0
        self._post_json(
            "/experiment/control",
            {
                "event": "stop",
                "experimentID": experiment_id,
                "eventUnixTime": start_unix + 2.0,
                "eventMonotonicTime": 102.0,
            },
        )
        self._post_json(
            "/experiment/control",
            {
                "event": "start",
                "experimentID": experiment_id,
                "eventUnixTime": start_unix,
                "eventMonotonicTime": 100.0,
            },
        )

        expected = self.root / readable_experiment_name(start_unix)
        matching = [
            directory
            for directory in self.root.iterdir()
            if directory.is_dir()
            and json.loads((directory / "experiment_state.json").read_text(encoding="utf-8"))[
                "experiment_id"
            ]
            == experiment_id
        ]
        self.assertEqual(matching, [expected])
        state = json.loads((expected / "experiment_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["start_unix_time"], start_unix)
        self.assertEqual(state["stop_unix_time"], start_unix + 2.0)

    def test_reconcile_repairs_stale_one_of_five_state_from_saved_files(self) -> None:
        experiment_id = "repair-complete-state"
        folder = self.root / "20260722-200000"
        folder.mkdir()
        for name in (
            "pose.csv",
            "sender_transport.csv",
            "video.mp4",
            "ultrawide_video.mp4",
            "capture_manifest.json",
        ):
            (folder / name).write_bytes(b"saved")
        (folder / "experiment_state.json").write_text(
            json.dumps({"experiment_id": experiment_id}), encoding="utf-8"
        )
        (folder / "upload_state.json").write_text(
            json.dumps(
                {
                    "capture_id": experiment_id,
                    "expected_files": 5,
                    "components": {"pose_csv": "pose.csv"},
                    "uploaded_components": ["pose_csv"],
                    "complete": False,
                }
            ),
            encoding="utf-8",
        )

        state = reconcile_upload_state(folder)

        self.assertTrue(state["complete"])
        self.assertEqual(
            set(state["components"]),
            {"pose_csv", "sender_transport", "video", "ultrawide_video", "manifest"},
        )
        self.assertEqual(len(state["uploaded_components"]), 5)

    def test_migration_recovers_leftover_uuid_video_fragment(self) -> None:
        experiment_id = "ABCDEF12-1234-5678-9ABC-DEF012345678"
        start_unix = 1_700_000_400.0
        readable = self.root / readable_experiment_name(start_unix)
        readable.mkdir()
        (readable / "experiment_state.json").write_text(
            json.dumps(
                {
                    "experiment_id": experiment_id,
                    "start_unix_time": start_unix,
                }
            ),
            encoding="utf-8",
        )
        (readable / "video.mp4").write_bytes(b"canonical")
        leftover = self.root / experiment_id
        leftover.mkdir()
        (leftover / "video.mp4").write_bytes(b"preserved-fragment")

        migrated = migrate_uuid_experiment_directories(self.root)

        self.assertEqual(migrated, [(leftover, readable)])
        self.assertFalse(leftover.exists())
        fragments = list((readable / "_merged_fragments" / experiment_id).glob("video*.mp4"))
        self.assertEqual(len(fragments), 1)
        self.assertEqual(fragments[0].read_bytes(), b"preserved-fragment")

    def test_zarr_contains_pose_and_all_five_magnetic_chips(self) -> None:
        folder = self.root / "zarr-source"
        folder.mkdir()
        start = 1_700_000_000.0
        (folder / "capture_manifest.json").write_text(
            json.dumps(
                {
                    "createdAtUnixTime": start,
                    "experimentStartUnixTime": start,
                    "experimentStartMonotonicTime": 100.0,
                    "sessionStartFrameTime": 100.0,
                }
            ),
            encoding="utf-8",
        )
        (folder / "pose.csv").write_text(
            "sequence,relative_time,x,y,z,qx,qy,qz,qw\n"
            "1,0.0,0,0,0,0,0,0,1\n"
            "2,0.1,1,2,3,0,0,0,1\n",
            encoding="utf-8",
        )
        magnetic_columns = [
            f"s{chip}_{axis}"
            for chip in range(5)
            for axis in ("t", "x", "y", "z")
        ]
        first_values = [str(chip * 10 + axis) for chip in range(5) for axis in range(4)]
        second_values = [str(chip * 10 + axis + 1) for chip in range(5) for axis in range(4)]
        (folder / "magnetic.csv").write_text(
            "relative_time," + ",".join(magnetic_columns) + "\n"
            "0.0," + ",".join(first_values) + "\n"
            "0.1," + ",".join(second_values) + "\n",
            encoding="utf-8",
        )

        exporter = AutoZarrExporter(image_size=64)
        self.assertTrue(exporter.schedule(folder, "zarr-test"))
        exporter.wait_for_all()
        output = folder / "dataset.zarr"
        root = zarr.open_group(str(output), mode="r")

        zarr_state = json.loads((folder / "zarr_state.json").read_text(encoding="utf-8"))
        self.assertEqual(zarr_state["status"], "complete")
        self.assertEqual(root["data/magnetic_txyz"].shape, (2, 5, 4))
        self.assertEqual(root["data/magnetic_valid"].shape, (2, 5))
        self.assertTrue(root["data/magnetic_valid"][:].all())
        self.assertAlmostEqual(float(root["data/magnetic_txyz"][1, 4, 3]), 44.0)

    def _post_json(self, path: str, value: dict) -> dict:
        body = json.dumps(value).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        self.assertEqual(response.status, 200, payload.decode(errors="replace"))
        return json.loads(payload)

    def _upload(
        self,
        experiment_id: str,
        component: str,
        filename: str,
        body: bytes,
        index: int,
        total: int,
    ) -> dict:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Capture-ID": experiment_id,
                "X-Capture-Component": component,
                "X-Original-Filename": filename,
                "X-Upload-Kind": "experiment",
                "X-Experiment-File-Index": str(index),
                "X-Experiment-File-Count": str(total),
            },
        )
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        self.assertEqual(response.status, 200, payload.decode(errors="replace"))
        return json.loads(payload)


if __name__ == "__main__":
    unittest.main()
