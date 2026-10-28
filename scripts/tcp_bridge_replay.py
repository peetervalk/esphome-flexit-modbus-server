"""
TCP Bridge Replay for Flexit Modbus Server
Replays a capture from tcp_bridge_capture.py or tcp_bridge_watch_command.py through
Python ports of

  - the RS485 echo filter of ESP-ModbusRTUServer (expectEcho, filterEcho,
    releaseEcho and clearEcho, as of commit 75e0881), and
  - the frame splitter of flexit_modbus_server.cpp (expected_frame_length and the
    onRawBuffer handler),

once with the echo filter on and once with it off. Each replay works out which
replies the ESP owes, and those are compared with the replies the ESP really sent
(the T blocks). This shows

  - whether the transceiver echoes: every frame sent comes back on RX,
  - whether the filter drops all of it without holding back anything else, and
  - whether the ESP behaves like the replay with the filter or the one without.

The bridge mirrors RX bytes before the library filters them, so the echo is in the
capture either way. Idle polling is parsed the same with and without the filter and
cannot tell the two apart. A command can: the reply to a single register read is
7 bytes, and the CS60 follows it with a 0x65 broadcast, which starts with 0x00. A
valid frame followed by 0x00 still has a valid CRC, so the echo plus that byte
passes as an 8-byte read request. Unfiltered, the ESP answers it with an exception
and loses the 0x65.

Not modelled: the filter's 50 ms timeout (held bytes are only released by a
mismatch) and time in general. Frames with bit 7 set in the function code are
counted but not dispatched.
"""

import argparse
import json
from collections import Counter
from typing import Optional

# flexit_modbus_server.h, and the sizes passed to ModbusRTUServer::begin()
MIN_FRAME_LENGTH = 5
MAX_NUM_COILS = 1000
MAX_NUM_HOLDING_REGISTERS = 0x160
NUM_INPUT_REGISTERS = 4

# ModbusRTUServer.h
ECHO_SIZE = 256
ECHO_MAX_FRAMES = 4

# The function codes the frame splitter knows
FUNCTION_CODES = {
    0x01: "Read Coils",
    0x03: "Read Holding Registers",
    0x04: "Read Input Registers",
    0x06: "Write Single Register",
    0x10: "Write Multiple Registers",
    0x65: "Custom Write (0x65)",
}


def calc_crc(data: bytes) -> int:
    """Calculate Modbus RTU CRC16."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def crc_ok(frame: bytes) -> bool:
    """ModbusRTUServer::checkCrc: the CRC over the frame and its CRC bytes is 0."""
    return len(frame) >= 4 and calc_crc(frame) == 0


def expected_frame_length(buf: bytearray, offset: int, available: int) -> int:
    """Port of expected_frame_length() in flexit_modbus_server.cpp."""
    function = buf[offset + 1]

    if function in (0x01, 0x03, 0x04, 0x06, 0x65):
        return 8
    if function == 0x10:
        if available < 7:
            return available + 1
        quantity = (buf[offset + 4] << 8) | buf[offset + 5]
        byte_count = buf[offset + 6]
        if quantity == 0 or quantity > 123 or byte_count != quantity * 2:
            return 0
        return byte_count + 9
    return 5 if function & 0x80 else 0


class EchoFilter:
    """Port of the library's echo filter. Bytes it lets through go to `buffer`."""

    def __init__(self, buffer: bytearray, enabled: bool):
        self.buffer = buffer
        self.enabled = enabled
        self.echo = bytearray()
        self.pos = 0
        self.frame_start = 0
        self.frame_ends = []
        self.frame_index = 0

        self.dropped_frames = 0
        self.dropped_bytes = 0
        self.releases = []          # (held bytes, expected byte, received byte)
        self.overflows = 0

    def clear(self):
        self.echo = bytearray()
        self.pos = 0
        self.frame_start = 0
        self.frame_ends = []
        self.frame_index = 0

    def release(self):
        self.buffer.extend(self.echo[self.frame_start:self.pos])
        self.clear()

    def expect(self, frame: bytes):
        """expectEcho(): called with every frame the ESP sends."""
        if not self.enabled or len(frame) > ECHO_SIZE:
            return

        if len(self.frame_ends) >= ECHO_MAX_FRAMES or len(self.echo) + len(frame) > ECHO_SIZE:
            self.overflows += 1
            self.clear()

        self.echo.extend(frame)
        self.frame_ends.append(len(self.echo))

    def feed(self, byte: int) -> bool:
        """filterEcho(): True when the byte is held back."""
        if self.pos >= len(self.echo):
            return False

        if byte != self.echo[self.pos]:
            held = bytes(self.echo[self.frame_start:self.pos])
            self.releases.append((held, self.echo[self.pos], byte))
            self.release()
            return False

        self.pos += 1

        if self.pos == self.frame_ends[self.frame_index]:
            self.dropped_frames += 1
            self.dropped_bytes += self.pos - self.frame_start
            self.frame_start = self.pos
            self.frame_index += 1

            if self.frame_index == len(self.frame_ends):
                self.clear()

        return True


class Splitter:
    """Port of the onRawBuffer handler, without the dispatch."""

    def __init__(self):
        self.buffer = bytearray()
        self.resync_bytes = 0

    def run(self) -> list[bytes]:
        """Returns the frames found, and keeps a trailing partial frame."""
        frames = []
        data = self.buffer
        offset = 0

        while len(data) - offset >= MIN_FRAME_LENGTH:
            available = len(data) - offset
            length = expected_frame_length(data, offset, available)

            if length == 0:
                offset += 1
                self.resync_bytes += 1
                continue
            if length > available:
                break

            frame = bytes(data[offset:offset + length])
            if not crc_ok(frame):
                offset += 1
                self.resync_bytes += 1
                continue

            frames.append(frame)
            offset += length

        del data[:offset]
        return frames


def expected_reply(frame: bytes, address: int) -> Optional[tuple[int, int]]:
    """Function code and length of the reply the ESP owes for this frame, if any."""
    if frame[0] != address:
        return None                 # a broadcast, or meant for another server

    function = frame[1]
    start = (frame[2] << 8) | frame[3]
    quantity = (frame[4] << 8) | frame[5]
    exception = (function | 0x80, 5)

    if function == 0x01:
        if quantity < 1 or quantity > 2000 or start + quantity > MAX_NUM_COILS:
            return exception
        return (function, 5 + ((quantity + 7) >> 3))
    if function == 0x03:
        if quantity < 1 or quantity > 125 or start + quantity > MAX_NUM_HOLDING_REGISTERS:
            return exception
        return (function, 5 + quantity * 2)
    if function == 0x04:
        if quantity < 1 or quantity > 125 or start + quantity > NUM_INPUT_REGISTERS:
            return exception
        return (function, 5 + quantity * 2)
    if function == 0x06:
        return exception if start >= MAX_NUM_HOLDING_REGISTERS else (function, 8)
    if function == 0x10:
        return exception if start + quantity > MAX_NUM_HOLDING_REGISTERS else (function, 8)
    if function == 0x65:
        return None                 # clears the command coil, no reply
    return exception


def function_name(function: int) -> str:
    return FUNCTION_CODES.get(function, f"Unknown (0x{function:02X})")


def describe(frame: bytes) -> str:
    """One line for a frame from the CS60."""
    function = frame[1]
    addr = (frame[2] << 8) | frame[3]
    value = (frame[4] << 8) | frame[5]
    name = function_name(function)

    if function in (0x06, 0x65):
        return f"ID:{frame[0]:02X} {name} Addr:0x{addr:04X} Val:{value}"
    return f"ID:{frame[0]:02X} {name} Addr:0x{addr:04X} Qty:{value}"


def describe_reply(frame: bytes) -> str:
    """One line for a reply from the ESP."""
    function = frame[1]

    if function & 0x80:
        return f"Exception 0x{function:02X} code {frame[2]}"
    if function in (0x03, 0x04):
        values = [(frame[i] << 8) | frame[i + 1] for i in range(3, 3 + frame[2], 2)]
        return f"Register reply {values}"
    return f"Reply 0x{function:02X} ({len(frame)} bytes)"


def short_hex(data: bytes, limit: int = 24) -> str:
    return data.hex() if len(data) <= limit else f"{data[:limit].hex()}... ({len(data)} bytes)"


def load_flushes(path: str) -> list[tuple[float, bytes, bytes]]:
    """Group the blocks into (time, T payload, R payload), one per reply."""
    with open(path) as f:
        blocks = [json.loads(line) for line in f if line.strip()]

    flushes = []
    i = 0
    while i < len(blocks) and blocks[i]["dir"] != "T":
        i += 1

    while i < len(blocks):
        t = blocks[i]["t"]
        tx = bytes.fromhex(blocks[i]["hex"])
        rx = b""
        if i + 1 < len(blocks) and blocks[i + 1]["dir"] == "R":
            rx = bytes.fromhex(blocks[i + 1]["hex"])
            i += 1
        flushes.append((t, tx, rx))
        i += 1

    # A capture can end between a T block and its R block. That reply has no
    # request to go with, so leave it out.
    if flushes and blocks[-1]["dir"] == "T":
        flushes.pop()

    return flushes


def replay(flushes: list, address: int, filter_enabled: bool) -> dict:
    splitter = Splitter()
    echo = EchoFilter(splitter.buffer, filter_enabled)

    owed = []               # (reply, request) the ESP still has to answer
    received = Counter()
    matched = 0
    mismatches = []         # (time, request, reply the ESP sent)
    unsolicited = []        # (time, reply the ESP sent)
    exception_frames = 0
    rx_bytes = 0
    coils = {}              # start address of the coil poll -> coils set
    events = []

    for index, (t, tx, rx) in enumerate(flushes):
        if index == 0:
            # The bridge starts mirroring RX when the client connects, so the first
            # R block can begin in the middle of a frame. Only its reply is used.
            echo.expect(tx)
            continue

        # Within a flush the R bytes were read before the T frame was written
        rx_bytes += len(rx)
        for byte in rx:
            if not echo.feed(byte):
                splitter.buffer.append(byte)

        for frame in splitter.run():
            if frame[1] & 0x80:
                exception_frames += 1
                events.append((t, f"RX  exception response {frame.hex()}, not dispatched"))
                continue

            broadcast = " (broadcast)" if frame[0] == 0 else ""
            received[function_name(frame[1]) + broadcast] += 1

            reply = expected_reply(frame, address)
            if reply:
                owed.append((reply, frame))
            # The coil polls and the status broadcasts repeat all the time
            if frame[1] not in (0x01, 0x10) and not (frame[1] == 0x06 and frame[0] == 0):
                events.append((t, "RX  " + describe(frame)))

        if not owed:
            unsolicited.append((t, tx))
        else:
            reply, request = owed.pop(0)
            if tx[0] != address or (tx[1], len(tx)) != reply or not crc_ok(tx):
                mismatches.append((t, request, tx))
            else:
                matched += 1
                if tx[1] == 0x01:
                    start = (request[2] << 8) | request[3]
                    now_set = [start + i * 8 + bit
                               for i, byte in enumerate(tx[3:3 + tx[2]])
                               for bit in range(8) if byte >> bit & 1]
                    if coils.get(start, []) != now_set:
                        listed = ", ".join(f"0x{coil:03X}" for coil in now_set) or "none"
                        events.append((t, f"TX  Coils set from {start}: {listed}"))
                    coils[start] = now_set
                else:
                    events.append((t, "TX  " + describe_reply(tx)))

        echo.expect(tx)

    return {
        "echo": echo,
        "splitter": splitter,
        "received": received,
        "matched": matched,
        "mismatches": mismatches,
        "unsolicited": unsolicited,
        "unanswered": len(owed),
        "exception_frames": exception_frames,
        "rx_bytes": rx_bytes,
        "events": events,
    }


def print_replay(title: str, result: dict, sent: int):
    echo = result["echo"]
    print(f"\n=== {title} ===")
    print(f"  RX bytes: {result['rx_bytes']}")
    if echo.enabled:
        print(f"  Echoes dropped: {echo.dropped_frames} of {sent} frames sent ({echo.dropped_bytes} bytes)")
        print(f"  Held bytes released after a mismatch: {len(echo.releases)}")
        for held, expected, got in echo.releases[:5]:
            print(f"    after {len(held)} bytes: expected {expected:02X}, got {got:02X}")
        if echo.overflows:
            print(f"  Echo buffer overflows: {echo.overflows}")
    print(f"  Bytes the splitter skipped to resync: {result['splitter'].resync_bytes}")
    print(f"  Frames received:")
    for name, count in sorted(result["received"].items()):
        print(f"    {name:38s} x{count}")
    if result["exception_frames"]:
        print(f"    Exception responses (bit 7 set)        x{result['exception_frames']}")
    print(f"  Replies owed that the ESP sent: {result['matched']}")
    print(f"  Replies that do not fit the request: {len(result['mismatches'])}")
    for t, request, tx in result["mismatches"][:5]:
        print(f"    {t:9.3f}  owed for {describe(request)}  sent {short_hex(tx)}")
    print(f"  Replies with no request: {len(result['unsolicited'])}")
    for t, tx in result["unsolicited"][:5]:
        print(f"    {t:9.3f}  sent {short_hex(tx)}")
    if result["unanswered"]:
        print(f"  Requests left unanswered at the end: {result['unanswered']}")


def main():
    parser = argparse.ArgumentParser(
        description="Replay a TCP bridge capture with and without the RS485 echo filter"
    )
    parser.add_argument(
        "capture",
        help="File from tcp_bridge_capture.py or tcp_bridge_watch_command.py"
    )
    parser.add_argument(
        "--address",
        type=int,
        default=1,
        help="Modbus address of the ESP (default: 1)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="List the command traffic: register reads, 0x65 and coil changes"
    )

    args = parser.parse_args()

    flushes = load_flushes(args.capture)
    if len(flushes) < 2:
        print("Not enough blocks in this capture.")
        return

    sent = Counter((tx[1], len(tx)) for _, tx, _ in flushes)
    print(f"{len(flushes)} replies from the ESP in {flushes[-1][0] - flushes[0][0]:.1f} s")
    for (function, length), count in sorted(sent.items()):
        print(f"  function 0x{function:02X}, {length} bytes  x{count}")

    with_filter = replay(flushes, args.address, True)
    without_filter = replay(flushes, args.address, False)
    # The first reply is only used for its echo
    print_replay("Replay with the echo filter", with_filter, len(flushes) - 1)
    print_replay("Replay without the echo filter", without_filter, len(flushes) - 1)

    fits_with = not with_filter["mismatches"] and not with_filter["unsolicited"]
    fits_without = not without_filter["mismatches"] and not without_filter["unsolicited"]

    print("\n=== Result ===")
    if fits_with and fits_without:
        print("  Both replays fit what the ESP sent, so this capture cannot tell them apart.")
        print("  Record a command with tcp_bridge_watch_command.py to find out.")
    elif fits_with:
        print("  The ESP behaves like the replay WITH the echo filter.")
    elif fits_without:
        print("  The ESP behaves like the replay WITHOUT the echo filter.")
    else:
        print("  Neither replay fits what the ESP sent.")

    if args.verbose:
        print("\n=== Command traffic (replay with the echo filter) ===")
        for t, text in with_filter["events"]:
            print(f"  {t:9.3f}  {text}")
        if not with_filter["events"]:
            print("  None.")


if __name__ == "__main__":
    main()
