import csv
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from capture_upload_server import create_upload_server
from experiment_data import ExperimentDataset, discover_experiments
from experiment_replay_ui import ReceiverDiagnosticsRecorder


class ExperimentWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.events: list[dict] = []
        self.server = create_upload_server("127.0.0.1", 0, self.root, self.events.append)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

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
        self._upload(experiment_id, "pose_csv", "pose.csv", pose, 1, 2)
        self._upload(experiment_id, "manifest", "capture_manifest.json", manifest, 2, 2)

        folder = self.root / experiment_id
        self.assertTrue((folder / "pose.csv").is_file())
        self.assertTrue((folder / "capture_manifest.json").is_file())
        state = json.loads((folder / "upload_state.json").read_text(encoding="utf-8"))
        self.assertTrue(state["complete"])
        self.assertEqual(state["components"]["pose_csv"], "pose.csv")
        self.assertTrue(any(event.get("event") == "start" for event in self.events))

        datasets = discover_experiments(self.root)
        self.assertEqual(len(datasets), 1)
        dataset = datasets[0]
        self.assertEqual(dataset.experiment_id, experiment_id)
        self.assertAlmostEqual(dataset.pose.times[0], 0.1)
        self.assertEqual(dataset.pose.nearest(0.1)["x"], "1")

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
