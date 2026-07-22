import argparse
import json
import re
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from experiment_zarr import AutoZarrExporter


UPLOAD_CHUNK_SIZE = 64 * 1024
COMPONENT_FILENAMES = {
    "pose_csv": "pose.csv",
    "magnetic_csv": "magnetic.csv",
    "sender_transport": "sender_transport.csv",
    "receiver_transport": "receiver_transport.csv",
    "manifest": "capture_manifest.json",
}
UUID_DIRECTORY_PATTERN = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


def get_default_upload_dir() -> Path:
    if getattr(sys, "frozen", False):
        # A PyInstaller one-file build extracts modules into a temporary
        # directory.  Store experiments next to the executable so they
        # survive after the monitor exits.
        return (Path(sys.executable).resolve().parent / "uploads").resolve()
    return (Path(__file__).resolve().parent / "uploads").resolve()


def console_print(*values, **kwargs) -> None:
    """Print when a console exists; PyInstaller --windowed sets it to None."""
    stream = kwargs.get("file", sys.stdout)
    if stream is None:
        return
    print(*values, **kwargs)


def read_json_file(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def experiment_directory_identity(directory: Path) -> str:
    experiment_state = read_json_file(directory / "experiment_state.json")
    upload_state = read_json_file(directory / "upload_state.json")
    manifest = read_json_file(directory / "capture_manifest.json")
    if not manifest:
        manifest = read_json_file(directory / "manifest__capture_manifest.json")
    return str(
        experiment_state.get("experiment_id")
        or upload_state.get("capture_id")
        or manifest.get("experimentID")
        or manifest.get("experiment_id")
        or ""
    )


def experiment_start_unix(directory: Path) -> float | None:
    manifest = read_json_file(directory / "capture_manifest.json")
    if not manifest:
        manifest = read_json_file(directory / "manifest__capture_manifest.json")
    experiment_state = read_json_file(directory / "experiment_state.json")
    value = (
        manifest.get("experimentStartUnixTime")
        or manifest.get("createdAtUnixTime")
        or experiment_state.get("start_unix_time")
    )
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def readable_experiment_name(start_unix: float) -> str:
    return datetime.fromtimestamp(start_unix).strftime("%Y%m%d-%H%M%S")


def find_experiment_directory(root: Path, capture_id: str) -> Path | None:
    direct = root / capture_id
    if direct.is_dir():
        return direct
    for directory in root.iterdir() if root.is_dir() else ():
        if directory.is_dir() and experiment_directory_identity(directory) == capture_id:
            return directory
    return None


def available_readable_directory(root: Path, start_unix: float, capture_id: str) -> Path:
    base_name = readable_experiment_name(start_unix)
    candidate = root / base_name
    suffix = 2
    while candidate.exists() and experiment_directory_identity(candidate) not in ("", capture_id):
        candidate = root / f"{base_name}_{suffix}"
        suffix += 1
    return candidate


def resolve_experiment_directory(
    root: Path,
    capture_id: str,
    start_unix: float | None = None,
) -> Path:
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    existing = find_experiment_directory(root, capture_id)
    if existing is not None:
        return existing
    if start_unix is not None:
        try:
            target = available_readable_directory(root, float(start_unix), capture_id)
        except (OSError, OverflowError, TypeError, ValueError):
            target = root / capture_id
    else:
        target = root / capture_id
    target.mkdir(parents=True, exist_ok=True)
    return target


def migrate_uuid_experiment_directories(root: Path) -> list[tuple[Path, Path]]:
    """Rename UUID experiment folders to local timestamp names without merging data."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        return []
    migrated: list[tuple[Path, Path]] = []
    for directory in sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name):
        if UUID_DIRECTORY_PATTERN.fullmatch(directory.name) is None:
            continue
        start_unix = experiment_start_unix(directory)
        if start_unix is None:
            continue
        capture_id = experiment_directory_identity(directory) or directory.name
        try:
            target = available_readable_directory(root, start_unix, capture_id)
        except (OSError, OverflowError, TypeError, ValueError):
            continue
        if target == directory:
            continue
        directory.rename(target)
        migrated.append((directory, target))
    return migrated


class UploadHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upload_root = get_default_upload_dir()
    upload_count = 0
    on_event: Optional[Callable[[dict], None]] = None
    zarr_exporter: AutoZarrExporter | None = None

    def handle_expect_100(self):
        self.send_response_only(100)
        self.end_headers()
        return True

    def do_POST(self):
        if self.path == "/experiment/control":
            self.handle_experiment_control()
            return
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
        expected_file_count = self._optional_positive_int_header("X-Experiment-File-Count")
        file_index = self._optional_positive_int_header("X-Experiment-File-Index")
        experiment_start_time = self._optional_float_header("X-Experiment-Start-Unix-Time")

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

        safe_capture_id = self.sanitize_token(capture_id)
        safe_filename = Path(original_filename).name
        if upload_kind == "experiment":
            target_dir = resolve_experiment_directory(
                self.upload_root,
                safe_capture_id,
                experiment_start_time,
            )
        else:
            target_dir = self.upload_root / safe_capture_id
            target_dir.mkdir(parents=True, exist_ok=True)

        file_path = target_dir / self.canonical_filename(component, safe_filename)
        temp_path = file_path.with_name(f"{file_path.name}.part")

        # Progress display
        timestamp = datetime.now().strftime("%H:%M:%S")
        size_text = f"{body_length / (1024 * 1024):.2f} MB" if body_length is not None else "chunked"
        console_print(f"\n[{timestamp}] Receiving upload...")
        console_print(f"  File: {original_filename}")
        console_print(f"  Type: {upload_kind}")
        console_print(f"  Size: {size_text}")
        console_print(f"  Progress: ", end="", flush=True)

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
            console_print(f" failed ({exc})")
            self.send_error(400, "Incomplete upload body")
            return

        if received <= 0 or (body_length is not None and received != body_length):
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            console_print(" failed")
            self.send_error(400, "Incomplete upload body")
            return

        temp_path.replace(file_path)

        UploadHandler.upload_count += 1
        state = self.update_upload_state(
            target_dir=target_dir,
            capture_id=safe_capture_id,
            component=component,
            filename=file_path.name,
            upload_kind=upload_kind,
            expected_file_count=expected_file_count,
            file_index=file_index,
        )
        if state.get("complete") and self.__class__.zarr_exporter is not None:
            self.__class__.zarr_exporter.schedule(
                target_dir,
                safe_capture_id,
                self.__class__.on_event,
            )

        console_print(f" Complete!")
        console_print(f"  Saved to: {file_path.resolve()}")
        console_print(f"  Total uploads: {UploadHandler.upload_count}")

        response = {
            "ok": True,
            "capture_id": safe_capture_id,
            "component": component,
            "upload_kind": upload_kind,
            "saved_to": str(file_path.resolve()),
            "complete": state.get("complete", False),
        }

        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True
        self.emit_event(
            {
                "type": "upload",
                "capture_id": safe_capture_id,
                "component": component,
                "path": str(file_path.resolve()),
                "complete": state.get("complete", False),
            }
        )

    def handle_experiment_control(self) -> None:
        content_length = self.headers.get("Content-Length")
        try:
            body_length = int(content_length or "0")
        except ValueError:
            self.send_error(400, "Invalid Content-Length")
            return
        if body_length <= 0 or body_length > 64 * 1024:
            self.send_error(400, "Invalid experiment control body")
            return

        try:
            payload = json.loads(self.rfile.read(body_length).decode("utf-8"))
            event = str(payload["event"]).lower()
            experiment_id = self.sanitize_token(str(payload["experimentID"]))
            event_unix_time = float(payload["eventUnixTime"])
            event_monotonic_time = float(payload["eventMonotonicTime"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.send_error(400, "Invalid experiment control payload")
            return

        if event not in {"start", "stop"}:
            self.send_error(400, "Experiment event must be start or stop")
            return

        target_dir = resolve_experiment_directory(
            self.upload_root,
            experiment_id,
            event_unix_time if event == "start" else None,
        )
        state_path = target_dir / "experiment_state.json"
        state = self.read_json(state_path)
        state.update(
            {
                "experiment_id": experiment_id,
                "updated_at": datetime.now().isoformat(timespec="milliseconds"),
            }
        )
        state[f"{event}_unix_time"] = event_unix_time
        state[f"{event}_monotonic_time"] = event_monotonic_time
        self.write_json_atomic(state_path, state)

        event_payload = {
            "type": "experiment_control",
            "event": event,
            "experiment_id": experiment_id,
            "event_unix_time": event_unix_time,
            "event_monotonic_time": event_monotonic_time,
            "directory": str(target_dir.resolve()),
        }
        self.emit_event(event_payload)
        self.send_json(200, {"ok": True, **event_payload})

    def update_upload_state(
        self,
        *,
        target_dir: Path,
        capture_id: str,
        component: str,
        filename: str,
        upload_kind: str,
        expected_file_count: Optional[int],
        file_index: Optional[int],
    ) -> dict:
        state_path = target_dir / "upload_state.json"
        state = self.read_json(state_path)
        components = state.get("components")
        if not isinstance(components, dict):
            components = {}
        components[component] = filename
        uploaded_components = state.get("uploaded_components")
        if not isinstance(uploaded_components, list):
            uploaded_components = []
        if component not in uploaded_components:
            uploaded_components.append(component)

        expected = expected_file_count or state.get("expected_files")
        state.update(
            {
                "capture_id": capture_id,
                "upload_kind": upload_kind,
                "updated_at": datetime.now().isoformat(timespec="milliseconds"),
                "expected_files": expected,
                "last_file_index": file_index,
                "components": components,
                "uploaded_components": uploaded_components,
                "complete": bool(expected and len(uploaded_components) >= int(expected)),
            }
        )
        self.write_json_atomic(state_path, state)
        return state

    @staticmethod
    def canonical_filename(component: str, original_filename: str) -> str:
        if component == "video":
            suffix = Path(original_filename).suffix.lower() or ".mp4"
            return f"video{suffix}"
        if component == "ultrawide_video":
            suffix = Path(original_filename).suffix.lower() or ".mp4"
            return f"ultrawide_video{suffix}"
        return COMPONENT_FILENAMES.get(component, f"{UploadHandler.sanitize_token(component)}__{original_filename}")

    @staticmethod
    def sanitize_token(value: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
        return sanitized or "unnamed"

    def _optional_positive_int_header(self, name: str) -> Optional[int]:
        raw_value = self.headers.get(name)
        if not raw_value:
            return None
        try:
            value = int(raw_value)
        except ValueError:
            return None
        return value if value > 0 else None

    def _optional_float_header(self, name: str) -> Optional[float]:
        raw_value = self.headers.get(name)
        if not raw_value:
            return None
        try:
            return float(raw_value)
        except ValueError:
            return None

    @staticmethod
    def read_json(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def write_json_atomic(path: Path, value: dict) -> None:
        temp_path = path.with_suffix(path.suffix + ".part")
        temp_path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(path)

    def send_json(self, status: int, value: dict) -> None:
        encoded = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True

    def emit_event(self, event: dict) -> None:
        callback = self.__class__.on_event
        if callback is not None:
            try:
                callback(event)
            except Exception as exc:
                console_print(f"[WARN] Upload event callback failed: {exc}", file=sys.stderr)

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
                console_print(f"{percent}%...", end="", flush=True)
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
                console_print(f"{received / (1024 * 1024):.1f}MB...", end="", flush=True)
                next_report += 1024 * 1024

        return received


def create_upload_server(
    host: str,
    port: int,
    upload_root: Path,
    on_event: Optional[Callable[[dict], None]] = None,
    *,
    auto_zarr: bool = True,
) -> ThreadingHTTPServer:
    class ConfiguredUploadHandler(UploadHandler):
        pass

    ConfiguredUploadHandler.upload_root = Path(upload_root).expanduser().resolve()
    ConfiguredUploadHandler.upload_root.mkdir(parents=True, exist_ok=True)
    migrate_uuid_experiment_directories(ConfiguredUploadHandler.upload_root)
    ConfiguredUploadHandler.on_event = staticmethod(on_event) if on_event is not None else None
    ConfiguredUploadHandler.zarr_exporter = AutoZarrExporter() if auto_zarr else None
    server = ThreadingHTTPServer((host, port), ConfiguredUploadHandler)
    server.daemon_threads = True
    if ConfiguredUploadHandler.zarr_exporter is not None:
        ConfiguredUploadHandler.zarr_exporter.backfill(
            ConfiguredUploadHandler.upload_root,
            ConfiguredUploadHandler.on_event,
        )
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Receive uploaded ARPoseStreamer files over HTTP.")
    parser.add_argument("--host", default="0.0.0.0", help="Host/IP to bind to.")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port to bind to.")
    parser.add_argument("--out-dir", default=str(get_default_upload_dir()), help="Directory to store uploaded files.")
    args = parser.parse_args()

    UploadHandler.upload_root = Path(args.out_dir).resolve()
    UploadHandler.upload_root.mkdir(parents=True, exist_ok=True)

    console_print("=" * 70)
    console_print("ARPoseStreamer Upload Server")
    console_print("=" * 70)
    console_print(f"Server URL: http://{args.host}:{args.port}")
    console_print(f"Upload folder: {UploadHandler.upload_root}")
    console_print(f"iPhone setup:")
    console_print(f"   1. Open ARPoseStreamer app")
    console_print(f"   2. Go to 'Past Records' page")
    console_print(f"   3. Select a recording and tap 'Upload'")
    console_print(f"   4. Enter server IP and port in app settings")
    console_print("=" * 70)
    console_print("Waiting for uploads...\n")

    try:
        server = create_upload_server(args.host, args.port, UploadHandler.upload_root)
        server.serve_forever()
    except KeyboardInterrupt:
        console_print("\n\nServer stopped by user")
        console_print(f"Total files received: {UploadHandler.upload_count}")
        sys.exit(0)


if __name__ == "__main__":
    main()
