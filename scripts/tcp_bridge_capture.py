"""
TCP Bridge Capture for Flexit Modbus Server
Records the raw TCP bridge blocks to a JSON-lines file, for tcp_bridge_replay.py.

Each line is one block: {"t": seconds since start, "wall": local time,
"dir": "T" or "R", "hex": payload}.

The bridge sends blocks when the ESP replies: first a T block with the reply, then
an R block with every byte read since the previous reply. An R block is therefore
not one frame. On a board that echoes it holds the echo of the previous reply, any
broadcasts and the request, back to back, and all of it was read before the T block
in front of it was written.
"""

import argparse
import json
import socket
import sys
import time
from datetime import datetime


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Receive exactly n bytes from socket."""
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("Connection closed by remote host")
        data += chunk
    return data


def main():
    parser = argparse.ArgumentParser(
        description="Record raw TCP bridge blocks to a JSON-lines file"
    )
    parser.add_argument(
        "--host",
        required=True,
        help="ESP IP address"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=502,
        help="TCP bridge port (default: 502)"
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=60,
        help="How long to record (default: 60)"
    )
    parser.add_argument(
        "--output",
        default="capture.jsonl",
        help="File to write (default: capture.jsonl)"
    )

    args = parser.parse_args()

    print(f"Connecting to {args.host}:{args.port}...")
    try:
        sock = socket.create_connection((args.host, args.port), timeout=5.0)
    except Exception as e:
        print(f"ERROR: Could not connect: {e}")
        sys.exit(1)

    start = time.monotonic()
    blocks = 0
    with open(args.output, "w") as f:
        try:
            while time.monotonic() - start < args.seconds:
                # 3-byte header: direction, then the payload length
                header = recv_exact(sock, 3)
                length = (header[1] << 8) | header[2]
                payload = recv_exact(sock, length)

                f.write(json.dumps({
                    "t": round(time.monotonic() - start, 4),
                    "wall": datetime.now().isoformat(timespec="milliseconds"),
                    "dir": chr(header[0]),
                    "hex": payload.hex(),
                }) + "\n")
                blocks += 1
        except KeyboardInterrupt:
            print("Stopped by user.")
        except (ConnectionError, socket.timeout) as e:
            print(f"ERROR: {e}")
        finally:
            sock.close()

    print(f"{blocks} blocks in {time.monotonic() - start:.1f} s -> {args.output}")


if __name__ == "__main__":
    main()
