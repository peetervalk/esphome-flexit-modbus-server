# CS60 bus analysis, September 2026

Findings from passively sniffing the RS485 bus between a Flexit CS60 and this
component acting as the panel, the fixes they suggest, and what has since
been done about them.

Hardware: ESP32 (GPIO18 TX / GPIO19 RX), ESPHome 2026.9.0, component at
`main` = 82065d3 (PR #25 merged). Captures taken 2026-09-22 01:22–01:39 local
time with `scripts/flexit_register_hunt.py`, which reads the `tcp_bridge`
stream and never writes to the bus.

Note on where the code lives: `ModbusRTUServer.cpp/.h` come from
`MSkjel/ESP-ModbusRTUServer` via `cg.add_library` in `__init__.py`, *unless* a
local copy sits in the component directory — in which case the local copy wins
and `cg.add_library` is skipped entirely. As of 2026-09-22 local copies are
present there, verified byte-for-byte identical to upstream `main` before being
patched, so library-side fixes now land in this repository too. They must be
**committed** for a build that pulls this repo via `external_components` to see
them; otherwise ESPHome silently falls back to fetching unpatched upstream.

Everything below is measured from the wire unless explicitly marked as a
hypothesis.

---

## 1. The bus spends about half its bandwidth in a self-sustaining exception loop

**Severity: high. Status: fixed 2026-09-22**, not yet confirmed on the wire.
This is the headline finding.

### Measurement

In one 60-second capture the ESP transmitted the byte sequence
`01 83 01 80 f0` — an exception response to FC03, code 1 (illegal function) —
**2748 times**, about 46 per second. A second variant, `01 83 02 c0 f1`
(illegal data address), appeared 462 times. Counted as literal byte sequences
in the raw TX stream, so these are not parser artifacts.

For comparison, the CS60's entire legitimate request rate over the same window
was ~2655 frames, i.e. ~44/s. The garbage traffic matches the real traffic
volume roughly one-for-one.

### Mechanism

1. **Seed.** The CS60 polls `FC03 start=0x0200 qty=377`. That is past the end of
   the register table (`MAX_NUM_HOLDING_REGISTERS = 0x160`) and far above the
   125-register cap in `handleReadHoldingRegisters`, so the component correctly
   answers with an exception: `01 83 02`.
2. **Echo.** This bus echoes our own transmissions back into RX, and the frame
   splitter in `flexit_modbus_server.cpp` explicitly accepts exception frames:

   ```c
   default:
     return (function & 0x80) ? 5 : 0;   // exception response, else unknown
   ```

   A 5-byte length is correct for *consuming* those bytes, but the splitter then
   passes the frame to `mb_.processFrame()` like any other.
3. **Conversion.** `data[1]` is `0x83`, which is not a case in the `processFrame`
   switch, so it reaches `default:` → `onInvalidFunction` → `sendException(0x83,
   0x01)`. Note the code changes here: the genuine code 2 becomes code **1**,
   because `onInvalidFunction` always passes `0x01`. This is why both variants
   appear on the wire, with code 1 dominating.
4. **Self-reproduction.** `sendException` builds `response[1] = function | 0x80`.
   For `0x83` **the high bit is already set, so `0x83 | 0x80 == 0x83`** — the
   reply is byte-identical to the frame that triggered it.
5. That reply echoes back too, is parsed again, and produces itself again. The
   loop is stable and never terminates.

So a single legitimate exception, answering a read the CS60 makes about once
every four seconds, is enough to seed a permanent ~46 frames/second loop.

### Evidence

Three consecutive bridge blocks, verbatim:

```
R len=20  01 83 01 80 f0 | 01 03 02 00 3f f8 54 | 01 03 01 14 00 01 c5 f2
          ^^^^^^^^^^^^^^ our own exception, echoed back into RX
T len=12  01 83 01 80 f0 | 01 03 02 00 01 79 84
          ^^^^^^^^^^^^^^ a NEW exception, emitted in reply to our own echo
R len=20  01 83 01 80 f0 | ...
          ^^^^^^^^^^^^^^ which echoes back again
```

Of 1702 RX blocks containing the echoed exception, 1251 are followed by a fresh
TX exception within two blocks.

### Fix applied, 2026-09-22

A response can never be a request, so the high bit in the function code is
enough to identify these. Three changes went in, across two files.

**1. The splitter consumes exception responses without processing them** —
`flexit_modbus_server.cpp`, in the `onRawBuffer` lambda in
`FlexitModbusServer::setup()`, the same place as the phantom-`0x10` guard from
PR #25. This is the cut that breaks the loop:

```c
if (!mb_.checkCrc(data + offset, len)) {   // right size, bad CRC -> resync
  ++offset;
  continue;
}

// Bit 7 set in the function code means this is an exception *response* -- ours,
// echoed back by the transceiver. Consume it so we stay in sync, but never
// answer it: sendException() re-sets bit 7, so a reply to 0x84 is 0x84 byte for
// byte, and a single echo becomes a permanent exception storm on the bus.
if ((data[offset + 1] & 0x80) == 0)
  mb_.processFrame(data + offset, len);

offset += len;
```

**2. `onInvalidFunction` masks the function code** — same file. A reply can now
never be byte-identical to the frame that prompted it, even if a response
reaches it by some path the splitter does not screen. This supersedes the
"have `sendException` refuse bit-7 frames" hardening suggested earlier, and is
better placed: it keeps the policy next to the bus-specific code rather than in
the generic library.

```c
mb_.sendException(function_code & 0x7F, 0x01, broadcast);
```

**3. `sendException` routes through `sendResponse`** — `ModbusRTUServer.cpp`.
`sendResponse` appends the CRC, drives TX enable, flushes, and screens broadcast
and a null stream; the hand-rolled body did only the CRC and the broadcast
check, writing straight to the stream:

```c
void ModbusRTUServer::sendException(uint8_t function, uint8_t exceptionCode, bool broadcast) {
    uint8_t response[5];
    response[0] = serverId_;
    response[1] = function | 0x80;
    response[2] = exceptionCode;

    sendResponse(response, 3, broadcast);
}
```

This was not part of the original suggestion and is worth calling out, because
it is a **behaviour change on the wire**: with a `tx_enable_pin` configured, the
old `sendException` wrote without asserting DE, so exceptions never reached the
bus at all. Fixing that is what makes them transmit for the first time on such
boards — so change 3 must not land without change 1, or the loop becomes
reachable on hardware where it previously was not. The captures above come from
a board with no `tx_enable_pin`, where exceptions did transmit and the loop was
live.

### Verifying the fix

Not yet done. Take a fresh 60-second capture and count the loop frame as a
literal byte sequence in the TX stream, the same way the 2748 figure above was
obtained:

```bash
python -u scripts/flexit_register_hunt.py --host <esp-ip> --duration 60 --save after.bin
```

Expect **zero** occurrences of `01 83 01 80 f0`, and the `01 83 02 c0 f1` count
to fall to roughly the CS60's own rate for the out-of-range `FC03 0x0200` poll
(~16 per 60 s per the table in §2) rather than 462. Unframeable bytes should
drop well below the 52 KB of 176 KB noted in §5.

### Still open

- **Echo suppression at the source** — the preferred root-cause fix, still not
  done. The guard above only screens frames *identifiable* as responses. A read
  response is not: an `FC01` reply with `byteCount == 3` is exactly 8 bytes with
  a valid CRC, indistinguishable from an 8-byte `FC01` request, so it is still
  re-dispatched as one. That path does not self-sustain as reliably as the
  exception loop, but it is the same class of bug, and it is what made the
  phantom-`0x10` stall fixed in PR #25 reachable in the first place. Dropping
  the next N received bytes after transmitting N would close the class; it has
  to be opt-in, since a transceiver that gates RE does not echo and we would
  swallow real bytes.
- **The `MODBUS_DISABLE_*` defines do nothing.** They sit at
  `flexit_modbus_server.h:22-25`, *after* `#include "ModbusRTUServer.h"` on line
  10, and `ModbusRTUServer.cpp` is a separate translation unit that never sees
  them. Nothing is passed via `cg.add_define` either. So every function code is
  compiled in: `FC04` dispatches to `handleReadInputRegisters` — which serves the
  4 input registers requested in `begin()` and answers illegal-data-address
  beyond them — rather than falling through to `onInvalidFunction`, and
  `FC05`/`FC0F`/`FC11` are live, meaning anything on the bus can write the
  command coils directly. Fixing this needs `cg.add_define` in `__init__.py`,
  since header ordering cannot reach the library's translation unit. Kept
  separate from the loop fix deliberately: it changes which function codes the
  firmware answers at all.
- **Neither file is compile-verified here.** The repository has no CI and no
  toolchain is installed locally, so the first real check is an ESPHome build.

---

## 2. The CS60 polls us for the setpoints; we are the authority

The component is not mirroring the unit's state. The CS60 **asks the panel**
what the setpoints should be, continuously. Over 60 seconds:

| Count | Request | Register |
|------:|---------|----------|
| 451 | `FC01 start=0x0000 qty=332` | command coils |
| 450 | `FC03 start=0x0003 qty=1` | `CMD_PERCENTAGE_SUPPLY_FAN_NORMAL` |
| 442 | `FC03 start=0x0008 qty=1` | `CMD_PERCENTAGE_EXTRACT_FAN_NORMAL` |
| 441 | `FC03 start=0x013F qty=1` | `CMD_HEATER` |
| 436 | `FC01 start=0x014C qty=360` | runtime-area coils |
| 435 | `FC03 start=0x0114 qty=1` | `CMD_TEMPERATURE_SUPPLY_AIR_CONTROL` |
| 16 | `FC03 start=0x0200 qty=377` | past the table; answered with an exception |

That is roughly seven polls per second per register.

**Consequence:** after a reset the holding-register array is zeroed, so the
component answers `0` to a question the CS60 asks seven times a second. This was
observed live: after a task-watchdog reboot, the HA number entities for
minimum / normal / maximum fan speed all read `0` while the unit continued
ventilating at 57% — the speed it had been commanded before the reboot, which it
simply kept.

The zero is not a chosen sentinel. `ModbusRTUServer::begin()` does
`new uint16_t[0x160]` followed by `std::fill_n(..., 0)`, and there is no
persistence layer anywhere: nothing reads flash, nothing survives a reset. A
register becomes non-zero only if the CS60 pushes it (the FC16 block below, or
the FC06 runtime counters) or if we write it. `0x03` is in neither pushed range,
so nobody but us ever writes it — and at boot we do not know it.

What the CS60 does with the answer is the one mechanism still not pinned down.
Two readings fit the captures. Either the read response is the delivery channel
and `0` is rejected as invalid, leaving the fan at its last commanded value; or a
bare read is never acted on at all and only a raised coil applies a value, making
whatever we answer inert until then. The two reconcile if the coil array is a
pending-command map: the CS60 polls coils (451×/60 s), reads the registers they
flag, applies, acknowledges with `0x65`, and the coil is cleared. One observation
separates them — does a `0x65` acknowledgement ever appear for a register written
with `write_holding_register` alone, value set and no coil? If never, the coil is
the gate, and whatever we serve while ignorant is harmless.

### What the CS60 sends us

- **Status block:** broadcast (address 0) `FC16 start=0x00BE qty=85 bc=170`,
  160 times in 60 s. Covers `0x00BE`–`0x0112`.
- **Runtime counters:** broadcast `FC06` to `0x0151`, `0x0155`, `0x015D`,
  `0x015F`, incrementing.
- **Command acknowledgement:** function `0x65`, which this component handles as
  "set the register and clear its coil". Captured examples, with counts from a
  60-second window during which the supply-fan setpoint was changed from 63 to
  59 from Home Assistant:

  | Frame | Meaning | Count |
  |-------|---------|------:|
  | `00 65 00 03 00 3b` | register `0x03` = 59 | 308 |
  | `00 65 00 03 00 3f` | register `0x03` = 63 (the previous value) | 131 |
  | `00 65 00 08 00 3b` | register `0x08` = 59 | 3 |
  | `00 65 01 14 00 01` | register `0x114` = 1 | 8 |
  | `00 65 01 3f 00 01` | register `0x13F` = 1 | 7 |

  So the CS60 confirms a commanded value back to us and keeps repeating the
  confirmation. It does **not** volunteer setpoints it was never commanded:
  in a capture taken 90 s after a reboot, with nothing yet commanded, no `0x65`
  frame for `0x03` appeared at all, and the register stayed 0.

### Register map addenda

Addresses inside the pushed `0x00BE`–`0x0112` block that carry non-zero data and
are not in the `HoldingRegisterIndex` enum:

| Register | Observed value |
|----------|----------------|
| `0x00C0` | 2221 (enum notes this as possibly MAX-timer related) |
| `0x00CB` | 18536 |
| `0x00CC` | 3 |
| `0x00FF` | 10 |
| `0x0100`–`0x0103` | 26981, 62048, 26981, 62607 — looks like two 32-bit values |
| `0x010F`, `0x0110` | 1, 1 |

`REG_TEMPERATURE_RETURN_WATER` (`0x00C6`) reads 48036 on this unit, i.e. no
sensor fitted; consumers should treat it as invalid rather than 4803.6 °C.

There is no register reporting **actual extract fan percentage** — only
`REG_PERCENTAGE_SUPPLY_FAN` (`0x00CA`). None of the candidate addresses above
tracked the extract fan.

---

## 3. The TCP bridge can reboot the ESP

**Severity: high**, because it is trivially reachable by anyone using the
feature for debugging.

`FlexitModbusServer::flush()` calls `client.write()` for every connected TCP
client, and `flush()` runs in the Modbus **transmit path**. A client that
connects and then stops reading its socket causes that write to block once the
TCP window fills, which stalls `loop()`.

Observed: a debugging client connected at 01:20 whose reader had stalled; at
01:21:53 — about 90 seconds later — the ESP rebooted with reset reason
**`task watchdog`**. The CS60 kept ventilating throughout, so the failure is
confined to the ESP, but the register mirror is lost on every such reboot
(see §2).

### Suggested fix

Never block the Modbus path on a network peer. Options, roughly in order of
preference:

- Check `client.availableForWrite()` before writing and skip or disconnect a
  client that cannot accept the data.
- Buffer bridge output and flush it from `loop()` outside the UART critical
  path, dropping the oldest data when the buffer is full.
- At minimum, document that the bridge must only be used with a client that
  drains continuously.

Until then, any tool consuming the bridge must drain the socket unconditionally.
`scripts/flexit_register_hunt.py` does this from a dedicated thread and discards
capture data rather than let the socket back up.

---

## 4. Cold-start seam for the fan setpoints (panel-side, YAML)

This replaces an earlier draft that proposed a full panel-side state machine with
the ESP as persistent authority for all three setpoints. Three things learned
since made most of that machinery unnecessary. They are recorded first, because
the design follows from them.

### What changed

**The CS60 stores no setpoints at all.** It asks the panel for them, acknowledges
a valid one with function `0x65`, and repeats the last one it received. There is
nothing to read back from it and nothing for it to volunteer — which is why a
capture 90 s after a reboot, with nothing commanded, showed no `0x65` for `0x03`
and the register stayed 0. When the writer is lost, the unit has no valid
setpoint, so nothing commands the fan and it holds its last commanded speed.

**`REG_PERCENTAGE_SUPPLY_FAN` (`0xCA`) is not a measurement.** These are EC
motors, and the register is the commanded 0-10 V signal the CS60 sends them; the
motor follows it. So while mode is Normal, `0xCA` *is* the effective normal
setpoint, readable straight off the bus. There is no ramp to wait out and no
sensor lag to filter, because it is a command rather than a reading. (Whether
airflow follows motor speed 1:1 is a property of the installation and out of
scope here.)

**The mode gate needs no extra guard.** Mode `0` is `"Stop"` and Normal is `2`,
and `REG_MODE` (`0xBF`) and `0xCA` both arrive in the same FC16 status block. So
before the first block lands the mode reads 0, the gate is shut, and a stale
`0xCA` cannot be latched; when the block arrives, both become valid in the same
frame. Gating on "mode is Normal" is sufficient by itself.

### Flash is a non-issue

The earlier draft worried about persisting a setpoint that an automation
recomputes every five minutes. It does not cost what it appears to.

`TemplateNumber::control()` persists, and it is the only thing that does:

```cpp
void TemplateNumber::control(float value) {
  this->set_trigger_.trigger(value);
  if (this->optimistic_) this->publish_state(value);
  if (this->restore_value_) this->pref_.save(&value);
}
```

`publish_state()` does not persist — the opposite of `switch_::Switch`, where
`publish_state` is exactly what writes. So publishing a value learned from `0xCA`
costs nothing; only a genuine set from HA or an automation reaches `control()`.

Those writes are deferred and deduplicated too. `ESP32PreferenceBackend::save()`
queues into `s_pending_save`, coalescing by key, and `sync()` calls
`is_changed_()`, which `memcmp`s against what NVS already holds and skips the
write when it matches. A setpoint recomputed every five minutes to the same
number writes nothing at all. At `DEBUG` this is visible as
`"Writing N items: X cached, Y written"`.

Worst case — a genuinely different value every five minutes — is 288 writes/day.
NVS appends 32-byte entries into 4096-byte pages and erases only when a page
fills, wear-levelled across the partition, so that is single-digit page erases
per day. Not a constraint.

Two further details of `TemplateNumber::setup()` shape the design:

- With `restore_value` set and nothing stored it falls back to `initial_value_`,
  so `initial_value: 60` supplies a cold-start default with no flash involved.
- It opens with `if (this->f_.has_value()) return;` — a number with a `lambda`
  never restores and never uses `initial_value`. Lambda and stored value are
  mutually exclusive, so an adopted value has to be pushed with an explicit
  `publish_state` rather than read by a lambda.

### Design

The automation that recomputes normal fan speed every five minutes is already the
state machine. What is missing is only the cold-start seam: the window between
boot and that automation's next run, during which we answer `0` and HA shows `0`.

Scope it honestly. The unit is unaffected either way — it holds its last
commanded speed. What the seam buys is that HA stops displaying a setpoint of
zero and we stop advertising one we do not mean. That is a correctness-of-display
problem with a bounded five-minute exposure, not a ventilation problem.

**Minimum and maximum** are constants on this installation, 40 and 90. The CS60
has no memory of them either, so until something commands them a Max-timer press
after a reboot finds nothing valid to act on. They need no learning: seed them at
boot and command them once.

**Normal** is the only value that moves, and the only one with a read-back:

1. At boot, serve the cold-start default (`initial_value: 60`), so we never
   answer zero.
2. Once the first status block shows mode Normal, `publish_state` the value of
   `0xCA` into the number. HA then shows what the unit is actually commanding,
   and nothing is written to flash.
3. If mode is not Normal, keep the default. `0xCA` reports the Min or Max speed
   in those modes and would latch the wrong number as Normal.
4. Stop there. The automation commands the real value on its next run.

Note what step 2 does *not* do: it does not command. After adopting, the unit is
still running uncommanded — correct by coincidence rather than by instruction.
Whether that matters is the first open question below.

### Open questions

- **Whether to command the adopted value at boot.** Commanding converts
  "coincidentally correct and uncommanded" into "commanded and known", which
  matters if anything can perturb the fan before the automation next runs. Not
  commanding keeps the ESP from writing to the unit on every reboot. The
  automation closes the gap within five minutes either way, which argues for
  leaving it out of the first version.
- **Whether the coil or the value gates application** — see the end of §2. If the
  coil is the gate, whatever we serve while ignorant is inert and step 1 above is
  purely cosmetic.
- **`min_value` disagreement.** `Fan Speed Normal` allows `0` while Min and Max
  start at `1`. Any guard that skips values below 1 would silently drop a
  legitimately stored zero for Normal. The three should agree.

### Notes and caveats

- **Untested.** This follows from the captures and from reading the ESPHome
  sources, but nothing here has been flashed or verified on hardware. The boot
  ordering in particular — that number entities restore before an `on_boot` block
  at priority `-100` runs — is assumed, and worth confirming against a boot log.
- Actual fan output stays a **sensor** on `0xCA`, separate from the setpoint
  number. Keeping intent and command echo as different entity types is what makes
  the "reads 0 after a reboot" confusion structurally impossible. There is no
  register reporting extract fan output, so only supply has a counterpart.
- **Compensation is the unit's own behaviour.** On this installation an external
  hardware switch (kitchen hood) makes the CS60 hold supply at normal and drop
  extract to minimum. We write supply and extract to the same value and let the
  CS60 apply compensation itself; nothing here should try to mirror or fight it.
- **Divergence detection**, floated in the earlier draft, is moot: with no
  setpoint stored in the unit there is no second copy to diverge from.
- **`getCoil` is not exposed** on `FlexitModbusServer`, though
  `ModbusRTUServer::getCoil` exists. Exposing it would allow a "command not yet
  acknowledged" diagnostic, since the CS60 clearing a coil is its
  acknowledgement — and would answer the coil-versus-value question from YAML.

---

## 5. Reproducing this

```bash
# Self-test the decoder (no hardware needed)
python scripts/flexit_register_hunt.py --selftest

# 60 s capture, saving the raw stream for later re-analysis
python -u scripts/flexit_register_hunt.py --host <esp-ip> --duration 60 \
    --save capture.bin --watch 59

# Re-analyse a saved capture against a different value
python scripts/flexit_register_hunt.py --replay capture.bin --watch 63
```

The bridge only mirrors RX while a client is attached, so start the capture
before triggering whatever you want to observe.

Interpreting the output: values listed as *pushed by CS60* are reliable.
Values listed as *served by ESP* come from pairing a response to a request, and
on a bus this noisy — roughly 52 KB of a 176 KB RX stream could not be framed,
mostly echo and the §1 loop — pairing for the four single-register polls is
best-effort. It fails safe (a response with no confidently matching request is
counted and dropped, not attributed), but treat single-register served values
with suspicion.
