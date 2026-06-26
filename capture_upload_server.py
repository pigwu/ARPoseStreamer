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
    protocol_version = "HTTP/1.1"
    upload_root = get_default_upload_dir()
    upload_count = 0

    def handle_expect_100(self):
        self.send_response_only(100)
        self.end_headers()
        return True

    def do_POST(self):
        if self.path != "/upload":
            self.send_error(404, "Unknown endpoint")
            return

        capture_id = self.headers.get("X-Capture-ID")
        component = self.headers.get("X-Capture-Component")
        original_filename = self.headers.get("X-Original-Filename")
        upload_kind = self.headers.get("X-Upload-Kind")
        content_length = self.headers.get("Content-Length")
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        is_chunked = "chunked" in transfer_encoding

        if not capture_id or not component or not original_filename or not upload_kind:
            self.send_error(400, "Missing required headers")
            return

        body_length = None
        if content_length:
            try:
                body_length = int(content_length)
            except ValueError:
                self.send_error(400, "Invalid Content-Length")
                return
            if body_length <= 0:
                self.send_error(400, "Empty upload body")
                return
        elif not is_chunked:
            self.send_error(411, "Missing Content-Length or chunked Transfer-Encoding")
            return

        safe_capture_id = capture_id.replace("/", "_").replace("\\", "_")
        safe_filename = Path(original_filename).name
        target_dir = self.upload_root / safe_capture_id
        target_dir.mkdir(parents=True, exist_ok=True)

        file_path = target_dir / f"{component}__{safe_filename}"
        temp_path = file_path.with_name(f"{file_path.name}.part")

        # Progress display
        timestamp = datetime.now().strftime("%H:%M:%S")
        size_text = f"{body_length / (1024 * 1024):.2f} MB" if body_length is not None else "chunked"
        print(f"\n[{timestamp}] Receiving upload...")
        print(f"  File: {original_filename}")
        print(f"  Type: {upload_kind}")
        print(f"  Size: {size_text}")
        print(f"  Progress: ", end="", flush=True)

        # Read with progress
        try:
            with temp_path.open("wb") as handle:
                if is_chunked and body_length is None:
                    received = self.read_chunked_body(handle)
                else:
                    received = self.read_fixed_body(handle, body_length)
        except Exception as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            print(f" failed ({exc})")
            self.send_error(400, "Incomplete upload body")
            return

        if received <= 0 or (body_length is not None and received != body_length):
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
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
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True

    def log_message(self, format, *args):
        return

    def read_fixed_body(self, handle, body_length: int) -> int:
        received = 0
        last_percent = -1

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

        return received

    def read_chunked_body(self, handle) -> int:
        received = 0
        next_report = 1024 * 1024

        while True:
            size_line = self.rfile.readline(128)
            if not size_line:
                raise ValueError("chunked upload ended before final chunk")

            size_text = size_line.split(b";", 1)[0].strip()
            try:
                chunk_size = int(size_text, 16)
            except ValueError as exc:
                raise ValueError(f"invalid chunk size: {size_text!r}") from exc

            if chunk_size == 0:
                while True:
                    trailer_line = self.rfile.readline(8192)
                    if trailer_line in (b"\r\n", b"\n", b""):
                        break
                break

            remaining = chunk_size
            while remaining > 0:
                chunk = self.rfile.read(min(UPLOAD_CHUNK_SIZE, remaining))
                if not chunk:
                    raise ValueError("chunked upload ended mid-chunk")
                handle.write(chunk)
                received += len(chunk)
                remaining -= len(chunk)

            line_end = self.rfile.read(2)
            if line_end != b"\r\n":
                raise ValueError("invalid chunk terminator")

            if received >= next_report:
                print(f"{received / (1024 * 1024):.1f}MB...", end="", flush=True)
                next_report += 1024 * 1024

        return received


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
