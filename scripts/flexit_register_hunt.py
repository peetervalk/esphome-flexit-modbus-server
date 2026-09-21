#!/usr/bin/env python3
"""
flexit_register_hunt.py - passive register-table capture for the Flexit CS60 bus.

The ESPHome tcp_bridge is a one-way sniffer, not a Modbus TCP server: it emits a
3-byte header ([dir 'T'|'R'][len_hi][len_lo]) followed by `len` raw UART bytes,
and never reads from the socket. So this tool can only watch - it cannot query
the CS60, and it can never write to the bus.

SAFETY - why the reader runs in its own thread
----------------------------------------------
The ESP writes to connected TCP clients from inside flush(), i.e. in the Modbus
TX path. A client that stops draining its socket therefore blocks the ESP's
loop(), and the task watchdog reboots it - observed on 2026-09-22, ~90 s after a
client connected with a stalled reader. So the socket is drained by a dedicated
thread that does nothing but recv(), and decoding happens on the main thread from
a bounded queue. If decoding cannot keep up, raw chunks are DROPPED (and counted)
rather than ever letting the socket back up. Losing capture data is always
preferable to rebooting the ventilation controller.

FRAMING - why CRC alone is not enough
-------------------------------------
Observed on the real bus: the RX stream contains our own transmissions echoed
back, merged into the same block as genuine CS60 frames, so direction does not
tell you request from response. Worse, our own coil response contains the bytes
`00 10 ... 80`, which a length-guessing parser reads as a broadcast FC16 write
with byte_count 0x80 and then waits 137 bytes for - the stall behind the
September 2026 TX outage. Frames are therefore accepted only when the function
code is known, the server address is plausible, and the length fields are
self-consistent (FC16 requires byte_count == 2 * quantity).

Usage:
  python flexit_register_hunt.py --host 192.168.1.240 --duration 30 --watch 43
  python flexit_register_hunt.py --replay capture.bin --watch 61
  python flexit_register_hunt.py --selftest
"""

import argparse
import re
import socket
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Modbus RTU basics
# --------------------------------------------------------------------------


def crc16(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def crc_ok(frame):
    if len(frame) < 4:
        return False
    want = crc16(frame[:-2])
    return frame[-2] == (want & 0xFF) and frame[-1] == (want >> 8)


FC_NAMES = {
    0x01: "ReadCoils",
    0x02: "ReadDiscreteInputs",
    0x03: "ReadHolding",
    0x04: "ReadInput",
    0x05: "WriteCoil",
    0x06: "WriteRegister",
    0x0F: "WriteCoils",
    0x10: "WriteRegisters",
    0x11: "ReportServerId",
    0x65: "FlexitCmdReset",
}

EXC_NAMES = {
    0x01: "illegal function",
    0x02: "illegal data address",
    0x03: "illegal data value",
    0x04: "server failure",
}

KNOWN_FCS = (0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x0F, 0x10, 0x11, 0x65)
MAX_FRAME = 256
# The component caps FC03 reads at 125 registers and holds 0x160 registers.
MAX_READ_QTY = 125
NUM_HOLDING_REGISTERS = 0x160

# --------------------------------------------------------------------------
# Register names, parsed from the component header so they cannot drift
# --------------------------------------------------------------------------


def load_register_names(header):
    if header is None:
        here = Path(__file__).resolve().parent
        header = (here.parent / "esphome" / "components" / "flexit_modbus_server"
                  / "flexit_modbus_server.h")
    names = {}
    try:
        text = header.read_text(encoding="utf-8", errors="replace")
    except OSError:
        print("WARNING: could not read %s; register names unavailable" % header,
              file=sys.stderr)
        return names
    pattern = r"^\s*(REG_[A-Z0-9_]+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*,"
    for name, value in re.findall(pattern, text, re.M):
        names.setdefault(int(value, 0), name[4:])  # strip the REG_ prefix
    return names


# --------------------------------------------------------------------------
# Frame reassembly
# --------------------------------------------------------------------------

REQ = "req"
RESP = "resp"
EXC = "exc"


def candidates(buf, allowed_addrs):
    """(total_length, kind) pairs worth CRC-testing at the head of buf.

    Only structurally self-consistent shapes are offered. Anything else is
    junk to resync past, which is what keeps a `00 10 .. 80` byte pair inside
    a coil response from being read as a broadcast FC16 write.
    """
    if len(buf) < 4:
        return []
    addr, fc = buf[0], buf[1]
    if addr not in allowed_addrs:
        return []
    out = []
    base = fc & 0x7F
    if fc & 0x80:
        # exception: [addr][fc|0x80][code][crc][crc]
        if base in KNOWN_FCS and buf[2] in EXC_NAMES:
            out.append((5, EXC))
        return out
    if base not in KNOWN_FCS:
        return out

    if base in (0x01, 0x02, 0x03, 0x04):
        qty = (buf[4] << 8) | buf[5] if len(buf) >= 6 else 0
        if 1 <= qty <= 2000:
            out.append((8, REQ))
        byte_count = buf[2]
        if byte_count > 0:
            out.append((5 + byte_count, RESP))
    elif base in (0x05, 0x06):
        out.append((8, REQ))       # a write and its echo are identical
    elif base in (0x0F, 0x10):
        qty = (buf[4] << 8) | buf[5] if len(buf) >= 6 else 0
        byte_count = buf[6] if len(buf) >= 7 else -1
        expect = 2 * qty if base == 0x10 else (qty + 7) // 8
        # This guard is the whole defence against the phantom-0x10 stall.
        if qty >= 1 and byte_count == expect:
            out.append((9 + byte_count, REQ))
        out.append((8, RESP))
    elif base == 0x65:
        out.append((8, REQ))       # Flexit command-reset: addr + value
    elif base == 0x11:
        byte_count = buf[2]
        if byte_count > 0:
            out.append((5 + byte_count, RESP))
    return [(t, k) for t, k in out if 4 <= t <= MAX_FRAME]


def plausible(frame, kind):
    """Second-stage sanity check on an otherwise CRC-valid frame."""
    base = frame[1] & 0x7F
    if kind == REQ and base in (0x03, 0x04):
        start = (frame[2] << 8) | frame[3]
        qty = (frame[4] << 8) | frame[5]
        # A read the component would answer, or one it would reject with an
        # exception - both are real traffic, so only nonsense is rejected.
        return qty >= 1 and start + qty <= 0x10000
    if kind == RESP and base in (0x03, 0x04):
        return frame[2] % 2 == 0 and frame[2] > 0
    return True


class FrameExtractor:
    """Pull validated frames out of one direction's byte stream."""

    def __init__(self, label, allowed_addrs):
        self.label = label
        self.allowed = allowed_addrs
        self.buf = bytearray()
        self.dropped = 0

    def feed(self, data):
        self.buf.extend(data)
        out = []
        while True:
            frame = self._take()
            if frame is None:
                return out
            out.append(frame)

    def _take(self, at_eof=False):
        while True:
            if len(self.buf) < 4:
                return None
            cands = candidates(bytes(self.buf), self.allowed)
            incomplete = False
            for total, kind in cands:
                if total > len(self.buf):
                    incomplete = True
                    continue
                frame = bytes(self.buf[:total])
                if crc_ok(frame) and plausible(frame, kind):
                    del self.buf[:total]
                    return frame, kind
            # Wait only when a structurally valid shape is genuinely still
            # arriving; never wait on junk, or one bad byte parks the parser.
            if cands and incomplete and not at_eof and len(self.buf) < MAX_FRAME:
                return None
            del self.buf[:1]
            self.dropped += 1

    def finish(self):
        out = []
        while True:
            frame = self._take(at_eof=True)
            if frame is None:
                return out
            out.append(frame)


# --------------------------------------------------------------------------
# Bus decoding
# --------------------------------------------------------------------------


class Colors:
    def __init__(self, enabled):
        self.on = enabled

    def __call__(self, text, code):
        return "\033[%sm%s\033[0m" % (code, text) if self.on else text


class BusDecoder:
    def __init__(self, names, watch, color, quiet):
        self.names = names
        self.watch = set(watch)
        self.c = color
        self.quiet = quiet
        self.pushed = {}     # written to us by the CS60 (its status block)
        self.served = {}     # values we answered a read with (our own mirror)
        self.coils = {}
        # Modbus RTU is strictly one transaction at a time, so exactly one
        # request may be outstanding. Keeping a queue instead mispairs the four
        # single-register polls (0x03, 0x08, 0x114, 0x13F) against each other
        # and invents values like "supply air control = 59". A single slot
        # fails safe: when frames are lost, a response goes unattributed
        # rather than landing on the wrong register.
        self.pending = deque(maxlen=1)    # (fc, start, qty) awaiting a response
        self.unpaired = 0
        self.recent_tx = deque(maxlen=32)
        self.hits = {}
        self.stats = {}
        self.exceptions = {}   # (fc, code, answered_request) -> count
        self.reads = {}        # (fc, start, qty) -> count
        self.echoes = 0
        self.baseline_until = 0.0
        self.noise_seen = set()

    # -- helpers ----------------------------------------------------------
    def reg_label(self, reg):
        name = self.names.get(reg)
        if name:
            return "0x%04X %s" % (reg, name)
        return "0x%04X %s" % (reg, self.c("UNMAPPED", "95"))

    def coil_label(self, coil):
        name = self.names.get(coil)
        return "%d (0x%02X)%s" % (coil, coil, " " + name if name else "")

    def bump(self, key):
        self.stats[key] = self.stats.get(key, 0) + 1

    # -- entry ------------------------------------------------------------
    def on_frame(self, ts, direction, frame, kind):
        addr, fc = frame[0], frame[1]
        base = fc & 0x7F
        self.bump("%s %-16s %s" % (direction, FC_NAMES.get(base, hex(base)), kind))

        if direction == "T":
            self.recent_tx.append((ts, frame))
        else:
            for t, prev in self.recent_tx:
                if prev == frame and ts - t < 2.0:
                    self.echoes += 1
                    self.bump("RX echo of our own TX")
                    return

        if kind == EXC:
            answered = "?"
            if self.pending:
                pfc, pstart, pqty = self.pending[-1]
                if pfc == base:
                    answered = "FC%02d start=0x%04X qty=%d" % (pfc, pstart, pqty)
                    self.pending.pop()
            key = (base, frame[2], answered)
            self.exceptions[key] = self.exceptions.get(key, 0) + 1
            if key not in self.noise_seen:
                self.noise_seen.add(key)
                self.emit(ts, direction, self.c(
                    "EXCEPTION to FC%02d: %s  (answering %s)" % (
                        base, EXC_NAMES.get(frame[2], frame[2]), answered), "91"))
            return

        if base == 0x10 and kind == REQ:
            start = (frame[2] << 8) | frame[3]
            qty = (frame[4] << 8) | frame[5]
            body = frame[7:7 + frame[6]]
            for i in range(qty):
                value = (body[2 * i] << 8) | body[2 * i + 1]
                self.set_reg(ts, direction, start + i, value, "cs60")
            self.bump("CS60 status push start=0x%04X qty=%d" % (start, qty))
        elif base == 0x06 and kind == REQ and direction == "R":
            reg = (frame[2] << 8) | frame[3]
            self.set_reg(ts, direction, reg, (frame[4] << 8) | frame[5], "cs60")
        elif base == 0x65 and direction == "R":
            reg = (frame[2] << 8) | frame[3]
            value = (frame[4] << 8) | frame[5]
            # The component treats 0x65 as "set register and clear its coil",
            # i.e. the CS60 acknowledging a command by resetting it.
            self.set_reg(ts, direction, reg, value, "cs60")
            self.set_coil(ts, direction, reg, False, poll=False)
        elif base in (0x01, 0x02, 0x03, 0x04) and kind == REQ:
            start = (frame[2] << 8) | frame[3]
            qty = (frame[4] << 8) | frame[5]
            self.pending.append((base, start, qty))
            key = (base, start, qty)
            self.reads[key] = self.reads.get(key, 0) + 1
            if base == 0x03 and (qty > MAX_READ_QTY
                                 or start + qty > NUM_HOLDING_REGISTERS):
                note = ("read we CANNOT serve (qty>%d or past 0x%X) -> exception"
                        % (MAX_READ_QTY, NUM_HOLDING_REGISTERS))
                if key not in self.noise_seen:
                    self.noise_seen.add(key)
                    self.emit(ts, direction, self.c(
                        "FC03 start=0x%04X qty=%d: %s" % (start, qty, note), "93"))
        elif base in (0x03, 0x04) and kind == RESP and direction == "T":
            start = self._match_response(base, frame[2] // 2)
            if start is not None:
                body = frame[3:3 + frame[2]]
                for i in range(len(body) // 2):
                    value = (body[2 * i] << 8) | body[2 * i + 1]
                    self.set_reg(ts, direction, start + i, value, "served")
        elif base == 0x01 and kind == RESP and direction == "T":
            # Several coil polls can be outstanding at once (the CS60 asks for
            # 0x0000+332 and 0x014C+360), so match on the byte count the
            # request implies - otherwise responses land on the wrong start
            # address and coils appear to flap.
            start = self._match_response(0x01, None, byte_count=frame[2])
            if start is not None:
                body = frame[3:3 + frame[2]]
                for byte_i, byte_val in enumerate(body):
                    for bit in range(8):
                        self.set_coil(ts, direction, start + byte_i * 8 + bit,
                                      bool(byte_val & (1 << bit)), poll=True)
        elif base == 0x05 and direction == "R":
            coil = (frame[2] << 8) | frame[3]
            on = ((frame[4] << 8) | frame[5]) == 0xFF00
            self.set_coil(ts, direction, coil, on, poll=False)

    def _match_response(self, fc, nregs, byte_count=None):
        """Pop the pending request this response belongs to; None if unknown."""
        if not self.pending:
            self.unpaired += 1
            return None
        entry = self.pending[-1]
        if (entry[0] != fc
                or (nregs is not None and entry[2] != nregs)
                or (byte_count is not None and (entry[2] + 7) // 8 != byte_count)):
            self.unpaired += 1
            return None
        self.pending.remove(entry)
        return entry[1]

    # -- tables -----------------------------------------------------------
    def set_reg(self, ts, direction, reg, value, source):
        table = self.pushed if source == "cs60" else self.served
        old = table.get(reg)
        table[reg] = value
        if value in self.watch:
            seen = self.hits.setdefault(value, [])
            if (reg, source) not in seen:
                seen.append((reg, source))
                self.emit(ts, direction, self.c(
                    "WATCH %d matched %s [%s]" % (value, self.reg_label(reg), source),
                    "1;93"))
        if old is None or old == value:
            return
        extra = ""
        name = self.names.get(reg, "")
        if "TEMPERATURE" in name and "CONTROL" not in name:
            extra = "  (%.1f -> %.1f C)" % (old / 10.0, value / 10.0)
        self.emit(ts, direction, "%s [%s] %s -> %s%s" % (
            self.reg_label(reg), source, old, self.c(str(value), "92"), extra))

    def set_coil(self, ts, direction, coil, on, poll):
        old = self.coils.get(coil)
        self.coils[coil] = on
        if old is None or old == on:
            return
        if not on and not poll:
            note = self.c("CS60 ACKed command", "1;92")
        elif on and not poll:
            note = "CS60 set coil"
        else:
            note = "seen while polling"
        self.emit(ts, direction, "coil %s %s -> %s  %s" % (
            self.coil_label(coil), "ON" if old else "OFF",
            "ON" if on else "OFF", note))

    # -- output -----------------------------------------------------------
    def emit(self, ts, direction, text):
        if self.quiet or ts < self.baseline_until:
            return
        stamp = datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
        tag = self.c("RX", "92") if direction == "R" else self.c("TX", "94")
        print("[%s] %s %s" % (stamp, tag, text))

    def print_table(self, title):
        print("\n===== %s =====" % title)
        for label, table in (("pushed by CS60 (its own status)", self.pushed),
                             ("served by ESP (our mirror)", self.served)):
            if not table:
                print("  (%s: nothing captured)" % label)
                continue
            print("  --- %s: %d registers ---" % (label, len(table)))
            for reg in sorted(table):
                value = table[reg]
                name = self.names.get(reg, "")
                extra = "   (%.1f C)" % (value / 10.0) if "TEMPERATURE" in name and "CONTROL" not in name else ""
                print("    %-44s = %6d%s" % (self.reg_label(reg), value, extra))
        if self.coils:
            on = sorted(c for c, v in self.coils.items() if v)
            print("  --- coils: %d seen, %d ON ---" % (len(self.coils), len(on)))
            for coil in on:
                print("    %s  ON  <-- command not yet acknowledged"
                      % self.coil_label(coil))

    def print_summary(self):
        print("\n===== frame counts =====")
        for key in sorted(self.stats):
            print("  %7d  %s" % (self.stats[key], key))
        if self.echoes:
            print("  %7d  RX frames that were echoes of our own TX" % self.echoes)
        if self.unpaired:
            print("\n  %d responses could not be matched to a request and were "
                  "left out of the served table (frames lost to bus noise)."
                  % self.unpaired)
        if self.reads:
            print("\n===== what the CS60 reads from us =====")
            for (fc, start, qty), n in sorted(self.reads.items(),
                                              key=lambda kv: -kv[1]):
                name = self.names.get(start, "")
                warn = ""
                if fc == 0x03 and (qty > MAX_READ_QTY
                                   or start + qty > NUM_HOLDING_REGISTERS):
                    warn = "  <-- we answer this with an exception"
                print("  %7d  FC%02d start=0x%04X qty=%-4d %s%s"
                      % (n, fc, start, qty, name, warn))
        if self.exceptions:
            print("\n===== exceptions we sent =====")
            for (fc, code, answered), n in sorted(self.exceptions.items(),
                                                  key=lambda kv: -kv[1]):
                print("  %7d  to FC%02d: %-22s answering %s"
                      % (n, fc, EXC_NAMES.get(code, code), answered))
        if self.watch:
            print("\n===== watched values =====")
            for value in sorted(self.watch):
                hits = self.hits.get(value)
                if not hits:
                    print("  %d: never seen in any register" % value)
                else:
                    for reg, source in hits:
                        print("  %d: %s [%s]" % (value, self.reg_label(reg), source))


# --------------------------------------------------------------------------
# Block framing and socket draining
# --------------------------------------------------------------------------


class BlockReader:
    """Split the bridge stream into [dir][len16] blocks, resyncing on junk."""

    def __init__(self):
        self.buf = bytearray()
        self.bad_headers = 0

    def feed(self, data):
        self.buf.extend(data)
        out = []
        while True:
            if len(self.buf) < 3:
                return out
            direction = self.buf[0]
            length = (self.buf[1] << 8) | self.buf[2]
            if direction not in (0x54, 0x52) or length == 0 or length > 4096:
                del self.buf[:1]
                self.bad_headers += 1
                continue
            if len(self.buf) < 3 + length:
                return out
            payload = bytes(self.buf[3:3 + length])
            del self.buf[:3 + length]
            out.append(("T" if direction == 0x54 else "R", payload))


class SocketDrainer(threading.Thread):
    """Drain the bridge socket relentlessly; never let the ESP block on us.

    The ESP writes to us from inside its Modbus TX path, so a client that stops
    reading trips its task watchdog. This thread therefore does nothing but
    recv() and hand chunks over, discarding the oldest data if the decoder
    cannot keep up.
    """

    daemon = True

    def __init__(self, sock, save_handle, max_queued_bytes=8 << 20):
        super().__init__(name="drainer")
        self.sock = sock
        self.save = save_handle
        self.max_queued = max_queued_bytes
        self.chunks = deque()
        self.queued = 0
        self.dropped_bytes = 0
        self.total = 0
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.error = None

    def run(self):
        try:
            while not self.closed.is_set():
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                if self.save:
                    self.save.write(chunk)
                with self.lock:
                    self.chunks.append(chunk)
                    self.queued += len(chunk)
                    self.total += len(chunk)
                    while self.queued > self.max_queued and self.chunks:
                        old = self.chunks.popleft()
                        self.queued -= len(old)
                        self.dropped_bytes += len(old)
        except OSError as exc:
            self.error = exc
        finally:
            self.closed.set()

    def pop_all(self):
        with self.lock:
            chunks = list(self.chunks)
            self.chunks.clear()
            self.queued = 0
        return chunks


def run_live(host, port, decoder, extractors, reader, save, duration, heartbeat):
    print("Connecting to %s:%d ..." % (host, port))
    sock = socket.create_connection((host, port), timeout=10.0)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    except OSError:
        pass
    drainer = SocketDrainer(sock, save)
    drainer.start()
    print("Connected; draining in a separate thread so the ESP can never block "
          "on us.\nThe bridge only mirrors RX while a client is attached, so "
          "start any test now.\n")
    start = time.time()
    last_beat = start
    try:
        while time.time() - start < duration:
            for chunk in drainer.pop_all():
                for direction, payload in reader.feed(chunk):
                    ts = time.time()
                    for frame, kind in extractors[direction].feed(payload):
                        decoder.on_frame(ts, direction, frame, kind)
            if drainer.closed.is_set() and not drainer.chunks:
                print("\nBridge closed the connection.")
                break
            now = time.time()
            if heartbeat and now - last_beat >= heartbeat:
                last_beat = now
                print("  ... %.0fs elapsed, %d KiB received, %d frames decoded"
                      % (now - start, drainer.total // 1024,
                         sum(decoder.stats.values())))
            time.sleep(0.02)
    finally:
        drainer.closed.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        drainer.join(timeout=2.0)
        print("\nDisconnected after %.0fs; %d KiB received." % (
            time.time() - start, drainer.total // 1024))
        if drainer.dropped_bytes:
            print("WARNING: dropped %d bytes because decoding fell behind "
                  "(socket was still drained, ESP never blocked)."
                  % drainer.dropped_bytes)
        if drainer.error:
            print("socket error: %s" % drainer.error)


def run_replay(path, decoder, extractors, reader):
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            for direction, payload in reader.feed(chunk):
                ts = time.time()
                for frame, kind in extractors[direction].feed(payload):
                    decoder.on_frame(ts, direction, frame, kind)


def drain_extractors(decoder, extractors):
    for direction, extractor in extractors.items():
        for frame, kind in extractor.finish():
            decoder.on_frame(time.time(), direction, frame, kind)


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------


def _frame(payload):
    crc = crc16(payload)
    return payload + bytes([crc & 0xFF, crc >> 8])


def selftest():
    names = load_register_names(None)
    decoder = BusDecoder(names, [43], Colors(False), quiet=False)
    allowed = {0, 1}
    extractors = {"T": FrameExtractor("T", allowed), "R": FrameExtractor("R", allowed)}

    # CS60 broadcast status push at 0xC3..0xCA, supply fan 45 then 43
    def push(fan):
        regs = [210, 0, 0, 0, 0, 0, 0, fan]
        return _frame(b"\x00\x10\x00\xC3\x00\x08\x10"
                      + b"".join(r.to_bytes(2, "big") for r in regs))

    push1, push2 = push(45), push(43)
    # CS60 polls our command block; we answer with normal = 43 at 0x03
    req = _frame(b"\x01\x03\x00\x00\x00\x05")
    resp = _frame(b"\x01\x03\x0A\x00\x02\x00\x00\x00\x00\x00\x2B\x00\x00")
    # CS60 clears coil 3 = acknowledges the normal-speed command
    clear = _frame(b"\x01\x05\x00\x03\x00\x00")
    # a read we cannot serve (qty > 125), and the exception we answer with
    bigread = _frame(b"\x01\x03\x00\x00\x01\x00")
    exc = _frame(b"\x01\x83\x02")
    # A real coil poll and its response. The response payload contains the byte
    # pair `00 10` followed later by `80`, exactly as the live bus does, and it
    # must NOT be read as a broadcast FC16 push with byte_count 0x80 - that
    # misparse is the phantom-0x10 stall.
    coil_req = _frame(b"\x01\x01\x00\x00\x01\x4C")     # start 0, qty 332
    coil_bits = bytearray(42)                          # byte_count 0x2A = 42
    coil_bits[37] = 0x10                               # -> coil 37*8+4 = 300 ON
    coil_bits[41] = 0x80                               # -> coil 41*8+7 = 335 ON
    coil_resp = _frame(b"\x01\x01\x2a" + bytes(coil_bits))

    stream = bytearray()

    def block(direction, data):
        stream.extend(bytes([ord(direction), len(data) >> 8, len(data) & 0xFF]))
        stream.extend(data)

    block("R", push1)
    block("R", push2[:5])              # split a frame across blocks
    block("R", push2[5:] + req)        # remainder merged with the next frame
    block("T", resp)
    block("R", b"\xAA\xBB" + clear)    # junk bytes force a resync
    block("R", resp)                   # our own TX echoed back into RX
    block("R", bigread)
    block("T", exc)
    block("R", coil_req)
    block("T", coil_resp)

    reader = BlockReader()
    for direction, payload in reader.feed(bytes(stream)):
        for frame, kind in extractors[direction].feed(payload):
            decoder.on_frame(time.time(), direction, frame, kind)
    drain_extractors(decoder, extractors)

    state = {"ok": True}

    def check(label, got, want):
        if got != want:
            state["ok"] = False
        print("  [%s] %s: got %r, want %r" % (
            "PASS" if got == want else "FAIL", label, got, want))

    print("self-test")
    check("0xCA pushed value", decoder.pushed.get(0xCA), 43)
    check("0xC3 pushed value (split frame)", decoder.pushed.get(0xC3), 210)
    check("served normal setpoint 0x03", decoder.served.get(0x03), 43)
    check("coil 3 ack seen", decoder.coils.get(3), False)
    check("resync dropped exactly the 2 junk bytes", extractors["R"].dropped, 2)
    check("echo detected once", decoder.echoes, 1)
    check("watch 43 hits", sorted(r for r, _s in decoder.hits.get(43, [])),
          [0x03, 0xCA])
    check("oversized read recorded", (0x03, 0x00, 256) in decoder.reads, True)
    check("exception recorded", sum(decoder.exceptions.values()), 1)
    check("exception paired to its request",
          list(decoder.exceptions)[0][2], "FC03 start=0x0000 qty=256")
    # the phantom-0x10 guard: no push may be invented from the coil payload,
    # so nothing outside 0xC3..0xCA may appear in the pushed table
    check("no phantom FC16 push from coil payload",
          sorted(r for r in decoder.pushed if not 0xC3 <= r <= 0xCA), [])
    check("coil response decoded as coils, not registers",
          decoder.coils.get(300), True)
    check("register names loaded", names.get(0xCA), "PERCENTAGE_SUPPLY_FAN")
    decoder.print_table("self-test table")
    decoder.print_summary()
    print("\nself-test:", "PASS" if state["ok"] else "FAIL")
    return 0 if state["ok"] else 1


# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(
        description="Passively capture the Flexit CS60 register table via the "
                    "ESP tcp_bridge.",
        epilog="The bridge cannot be written to; this tool only ever reads.")
    ap.add_argument("--host", help="ESP IP address")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--replay", type=Path, help="parse a saved capture instead")
    ap.add_argument("--save", type=Path, help="write the raw stream for --replay")
    ap.add_argument("--header", type=Path, help="flexit_modbus_server.h location")
    ap.add_argument("--watch", type=int, action="append", default=[],
                    help="flag any register that takes this value (repeatable)")
    ap.add_argument("--addr", type=int, action="append", default=[],
                    help="server addresses to accept (default: 0 and 1)")
    ap.add_argument("--baseline", type=float, default=0.0,
                    help="seconds to build the table silently before streaming")
    ap.add_argument("--duration", type=float, default=30.0,
                    help="how long to capture, seconds (default: 30)")
    ap.add_argument("--heartbeat", type=float, default=10.0,
                    help="progress line interval, 0 to disable")
    ap.add_argument("--quiet", action="store_true", help="tables only")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    # Unbuffered-ish output: a stalled pipe must never look like a stalled bus.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    if args.selftest:
        return selftest()
    if not args.host and not args.replay:
        ap.error("need --host or --replay (or --selftest)")

    color = Colors(not args.no_color and sys.stdout.isatty())
    names = load_register_names(args.header)
    decoder = BusDecoder(names, args.watch, color, args.quiet)
    allowed = set(args.addr) if args.addr else {0, 1}
    extractors = {"T": FrameExtractor("T", allowed),
                  "R": FrameExtractor("R", allowed)}
    reader = BlockReader()

    if args.baseline:
        decoder.baseline_until = time.time() + args.baseline
        print("Building a baseline for %gs (changes suppressed) ..." % args.baseline)

    save_handle = args.save.open("wb") if args.save else None
    try:
        if args.replay:
            run_replay(args.replay, decoder, extractors, reader)
        else:
            run_live(args.host, args.port, decoder, extractors, reader,
                     save_handle, args.duration, args.heartbeat)
        drain_extractors(decoder, extractors)
    except KeyboardInterrupt:
        print("\n(stopped)")
    finally:
        if save_handle:
            save_handle.close()
            print("Raw capture written to %s" % args.save)

    decoder.print_table("register table")
    decoder.print_summary()
    dropped = extractors["R"].dropped + extractors["T"].dropped
    if dropped or reader.bad_headers:
        print("\nresync: dropped %d RX and %d TX bytes no valid frame could "
              "claim, %d bad block headers"
              % (extractors["R"].dropped, extractors["T"].dropped,
                 reader.bad_headers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
