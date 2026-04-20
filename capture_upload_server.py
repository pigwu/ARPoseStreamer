import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


class UploadHandler(BaseHTTPRequestHandler):
    upload_root = Path("uploads")

    def do_POST(self):
        if self.path != "/upload":
            self.send_error(404, "Unknown endpoint")
            return

        capture_id = self.headers.get("X-Capture-ID")
        component = self.headers.get("X-Capture-Component")
        original_filename = self.headers.get("X-Original-Filename")
        upload_kind = self.headers.get("X-Upload-Kind")
        content_length = self.headers.get("Content-Length")

        if not capture_id or not component or not original_filename or not upload_kind or not content_length:
            self.send_error(400, "Missing required headers")
            return

        try:
            body_length = int(content_length)
        except ValueError:
            self.send_error(400, "Invalid Content-Length")
            return

        safe_capture_id = capture_id.replace("/", "_").replace("\\", "_")
        safe_filename = Path(original_filename).name
        target_dir = self.upload_root / safe_capture_id
        target_dir.mkdir(parents=True, exist_ok=True)

        file_path = target_dir / f"{component}__{safe_filename}"
        file_path.write_bytes(self.rfile.read(body_length))

        response = {
            "ok": True,
            "capture_id": safe_capture_id,
            "component": component,
            "upload_kind": upload_kind,
            "saved_to": str(file_path.resolve()),
        }

        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Receive uploaded ARPoseStreamer files over HTTP.")
    parser.add_argument("--host", default="0.0.0.0", help="Host/IP to bind to.")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port to bind to.")
    parser.add_argument("--out-dir", default="uploads", help="Directory to store uploaded files.")
    args = parser.parse_args()

    UploadHandler.upload_root = Path(args.out_dir)
    UploadHandler.upload_root.mkdir(parents=True, exist_ok=True)

    server = HTTPServer((args.host, args.port), UploadHandler)
    print(f"Upload server listening on http://{args.host}:{args.port}")
    print(f"Saving uploaded files under: {UploadHandler.upload_root.resolve()}")
    server.serve_forever()


if __name__ == "__main__":
    main()
