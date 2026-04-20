import argparse
import csv
import socket
import struct
import time
from pathlib import Path


FLOAT32_PACKET = struct.Struct("<Id7f")


def decode_packet(packet: bytes, encoding: str) -> tuple[int, float, float, float, float, float, float, float, float]:
    if encoding == "binary":
        if len(packet) != FLOAT32_PACKET.size:
            raise ValueError(f"Expected {FLOAT32_PACKET.size} bytes, got {len(packet)}")
        return FLOAT32_PACKET.unpack(packet)

    text = packet.decode("utf-8").strip()
    fields = text.split(",")
    if len(fields) != 9:
        raise ValueError(f"Expected 9 CSV values, got {len(fields)}")

    sequence = int(fields[0])
    values = [float(x) for x in fields[1:]]
    return (sequence, *values)


def main() -> None:
    parser = argparse.ArgumentParser(description="Receive AR pose packets over UDP.")
    parser.add_argument("--host", default="0.0.0.0", help="Host/IP to bind to.")
    parser.add_argument("--port", type=int, default=5555, help="UDP port to bind to.")
    parser.add_argument(
        "--encoding",
        choices=("binary", "csv"),
        default="binary",
        help="Expected packet encoding from the iPhone sender.",
    )
    parser.add_argument(
        "--csv-log",
        type=Path,
        default=None,
        help="Optional CSV file path for logging received samples.",
    )
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.host, args.port))

    csv_file = None
    csv_writer = None
    if args.csv_log is not None:
        args.csv_log.parent.mkdir(parents=True, exist_ok=True)
        csv_file = args.csv_log.open("w", newline="", encoding="utf-8")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(
            [
                "recv_time",
                "sequence",
                "sender_time",
                "x",
                "y",
                "z",
                "qx",
                "qy",
                "qz",
                "qw",
                "approx_latency_ms",
            ]
        )

    prev_recv_time = None
    prev_sequence = None
    print(f"Listening for UDP pose packets on {args.host}:{args.port} ({args.encoding})")

    try:
        while True:
            packet, address = sock.recvfrom(4096)
            recv_time = time.time()
            monotonic_recv_time = time.monotonic()
            sequence, sender_time, x, y, z, qx, qy, qz, qw = decode_packet(packet, args.encoding)
            approx_latency_ms = max(0.0, (recv_time - sender_time) * 1000.0)

            if prev_recv_time is None:
                fps = 0.0
            else:
                fps = 1.0 / max(monotonic_recv_time - prev_recv_time, 1e-9)
            prev_recv_time = monotonic_recv_time

            dropped = 0 if prev_sequence is None else max(0, sequence - prev_sequence - 1)
            prev_sequence = sequence

            print(
                f"{address[0]}:{address[1]} "
                f"seq={sequence:6d} drop={dropped:3d} "
                f"approx_lat={approx_latency_ms:7.2f}ms fps={fps:6.2f} "
                f"x={x:+.4f} y={y:+.4f} z={z:+.4f} "
                f"qx={qx:+.4f} qy={qy:+.4f} qz={qz:+.4f} qw={qw:+.4f}"
            )

            if csv_writer is not None:
                csv_writer.writerow([f"{recv_time:.6f}", sequence, sender_time, x, y, z, qx, qy, qz, qw, f"{approx_latency_ms:.3f}"])
                csv_file.flush()
    finally:
        if csv_file is not None:
            csv_file.close()
        sock.close()


if __name__ == "__main__":
    main()
