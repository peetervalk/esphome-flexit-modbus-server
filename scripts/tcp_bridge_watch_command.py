"""
TCP Bridge Command Watcher for Flexit Modbus Server
Waits on the TCP bridge until a command goes by, records it and stops.

A command shows up as a set bit in one of the ESP's coil replies (function 0x01).
The CS60 then reads the register and confirms with 0x65, which clears the bit.
The file holds the seconds before the command and everything up to a few seconds
after the coils are clear again, in the format of tcp_bridge_capture.py, for
tcp_bridge_replay.py. Nothing is written if no command is seen.

Trigger the command yourself, for example by changing the fan speed.
"""

import argparse
import json
import socket
import sys
import time
from collections import deque
from datetime import datetime

# Blocks kept from before the command (about 10 s of idle polling)
BACKLOG_BLOCKS = 400
# Recorded after the coils are clear again
TAIL_SECONDS = 3
# Coils still set after this long: the CS60 is not confirming
ACK_TIMEOUT_SECONDS = 30


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
        description="Record one command handshake from the TCP bridge"
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
        "--max-seconds",
        type=float,
        default=600,
        help="Give up when no command is seen for this long (default: 600)"
    )
    parser.add_argument(
        "--output",
        default="command.jsonl",
        help="File to write (default: command.jsonl)"
    )

    args = parser.parse_args()

    print(f"Connecting to {args.host}:{args.port}...")
    try:
        sock = socket.create_connection((args.host, args.port), timeout=5.0)
    except Exception as e:
        print(f"ERROR: Could not connect: {e}")
        sys.exit(1)

    print("Connected! Waiting for a command...")

    start = time.monotonic()
    backlog = deque(maxlen=BACKLOG_BLOCKS)
    coils_set = {}      # byte count of the coil reply -> any bit set
    set_at = None
    clear_at = None
    result = "No command seen."
    f = None

    try:
        while True:
            now = time.monotonic() - start
            if set_at is None and now > args.max_seconds:
                break

            header = recv_exact(sock, 3)
            payload = recv_exact(sock, (header[1] << 8) | header[2])
            record = {
                "t": round(time.monotonic() - start, 4),
                "wall": datetime.now().isoformat(timespec="milliseconds"),
                "dir": chr(header[0]),
                "hex": payload.hex(),
            }
            if f:
                f.write(json.dumps(record) + "\n")
            else:
                backlog.append(record)

            # The CS60 polls the coils in two requests, told apart by the byte count
            if record["dir"] == "T" and len(payload) > 5 and payload[1] == 0x01:
                coils_set[payload[2]] = any(payload[3:3 + payload[2]])
                pending = any(coils_set.values())

                if pending and set_at is None:
                    set_at = now
                    # Start the file on a T block, so that every R block follows its T
                    while backlog and backlog[0]["dir"] != "T":
                        backlog.popleft()
                    f = open(args.output, "w")
                    for earlier in backlog:
                        f.write(json.dumps(earlier) + "\n")
                    print(f"Command coil set at {record['wall']}")

                if set_at is not None and clear_at is None and not pending:
                    clear_at = now
                    result = f"Command confirmed: coils clear {clear_at - set_at:.2f} s after they were set."

            if clear_at is not None and now - clear_at > TAIL_SECONDS:
                break
            if set_at is not None and clear_at is None and now - set_at > ACK_TIMEOUT_SECONDS:
                result = f"Coils still set after {ACK_TIMEOUT_SECONDS} s: the command was NOT confirmed."
                break
    except KeyboardInterrupt:
        result = "Stopped by user."
    except (ConnectionError, socket.timeout) as e:
        result = f"ERROR: {e}"
    finally:
        sock.close()
        if f:
            f.close()

    print(result)
    if f:
        print(f"Recorded to {args.output}")


if __name__ == "__main__":
    main()
