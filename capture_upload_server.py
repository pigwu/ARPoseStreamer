import argparse
import json
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


UPLOAD_CHUNK_SIZE = 64 * 1024


def get_default_upload_dir() -> Path:
    return (Path(__file__).resolve().parent / "uploads").resolve()


class UploadHandler(BaseHTTPRequestHandler):
    upload_root = get_default_upload_dir()
    upload_count = 0

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
        if body_length <= 0:
            self.send_error(400, "Empty upload body")
            return

        safe_capture_id = capture_id.replace("/", "_").replace("\\", "_")
        safe_filename = Path(original_filename).name
        target_dir = self.upload_root / safe_capture_id
        target_dir.mkdir(parents=True, exist_ok=True)

        file_path = target_dir / f"{component}__{safe_filename}"
        temp_path = file_path.with_name(f"{file_path.name}.part")

        # Progress display
        timestamp = datetime.now().strftime("%H:%M:%S")
        size_mb = body_length / (1024 * 1024)
        print(f"\n[{timestamp}] Receiving upload...")
        print(f"  File: {original_filename}")
        print(f"  Type: {upload_kind}")
        print(f"  Size: {size_mb:.2f} MB")
        print(f"  Progress: ", end="", flush=True)

        # Read with progress
        received = 0
        last_percent = -1

        with temp_path.open("wb") as handle:
            while received < body_length:
                remaining = body_length - received
                to_read = min(UPLOAD_CHUNK_SIZE, remaining)
                chunk = self.rfile.read(to_read)
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)

                percent = int((received / body_length) * 100) if body_length else 100
                if percent != last_percent and percent % 10 == 0:
                    print(f"{percent}%...", end="", flush=True)
                    last_percent = percent

        if received != body_length:
            temp_path.unlink(missing_ok=True)
            print(" failed")
            self.send_error(400, "Incomplete upload body")
            return

        temp_path.replace(file_path)

        UploadHandler.upload_count += 1

        print(f" Complete!")
        print(f"  Saved to: {file_path.resolve()}")
        print(f"  Total uploads: {UploadHandler.upload_count}")

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
    parser.add_argument("--out-dir", default=str(get_default_upload_dir()), help="Directory to store uploaded files.")
    args = parser.parse_args()

    UploadHandler.upload_root = Path(args.out_dir).resolve()
    UploadHandler.upload_root.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("ARPoseStreamer Upload Server")
    print("=" * 70)
    print(f"Server URL: http://{args.host}:{args.port}")
    print(f"Upload folder: {UploadHandler.upload_root}")
    print(f"iPhone setup:")
    print(f"   1. Open ARPoseStreamer app")
    print(f"   2. Go to 'Past Records' page")
    print(f"   3. Select a recording and tap 'Upload'")
    print(f"   4. Enter server IP and port in app settings")
    print("=" * 70)
    print("Waiting for uploads...\n")

    try:
        server = HTTPServer((args.host, args.port), UploadHandler)
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\nServer stopped by user")
        print(f"Total files received: {UploadHandler.upload_count}")
        sys.exit(0)


if __name__ == "__main__":
    main()
