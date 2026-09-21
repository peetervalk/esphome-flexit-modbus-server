# CS60 bus analysis, September 2026

Findings from passively sniffing the RS485 bus between a Flexit CS60 and this
component acting as the panel, plus the fixes they suggest.

Hardware: ESP32 (GPIO18 TX / GPIO19 RX), ESPHome 2026.9.0, component at
`main` = 82065d3 (PR #25 merged). Captures taken 2026-09-22 01:22–01:39 local
time with `scripts/flexit_register_hunt.py`, which reads the `tcp_bridge`
stream and never writes to the bus.

Note that `ModbusRTUServer.cpp/.h` are not tracked in this repository; they come
from `MSkjel/ESP-ModbusRTUServer` via `cg.add_library`, unless a local copy sits
in the component directory, which then takes precedence. The fixes below say
which repository they belong to.

Everything below is measured from the wire unless explicitly marked as a
hypothesis.

---

## 1. The bus spends about half its bandwidth in a self-sustaining exception loop

**Severity: high.** This is the headline finding.

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

### Suggested fix

A response can never be a request, so the high bit in the function code is
enough to identify these. The fix belongs in this repository's frame splitter,
in the `parse_frames`-style lambda in `FlexitModbusServer::setup()`, which is
also where the related phantom-`0x10` guard from PR #25 lives. Keep consuming
the 5 bytes so the splitter stays in sync, but never process them:

```c
if (!mb_.checkCrc(data + offset, len)) {   // right size, bad CRC -> resync
  ++offset;
  continue;
}

// Our own transmissions are echoed back into RX on this bus. An exception
// response is never a request, and feeding one to processFrame() makes
// onInvalidFunction reply with `function | 0x80` - which for an already-set
// high bit reproduces the frame byte for byte, looping forever. Consume it to
// stay in sync, but do not process it.
if ((data[offset + 1] & 0x80) != 0) {
  offset += len;
  continue;
}

mb_.processFrame(data + offset, len);
```

Note on where the code lives: `ModbusRTUServer.cpp/.h` are **not part of this
repository**. `__init__.py` pulls them from
`https://github.com/MSkjel/ESP-ModbusRTUServer.git` unless a local copy is
present in the component directory, in which case the local copy wins. So a fix
inside `processFrame` or `sendException` would have to go to that repository,
whereas the splitter patch above lands here — and arguably belongs here anyway,
since the own-TX echo is a trait of this bus rather than a generic Modbus server
concern.

Two optional hardenings, once the above is in place:

- Have `sendException` refuse to answer a frame whose function code already has
  bit 7 set (in ESP-ModbusRTUServer), as a guard against any other path.
- Suppress the own-TX echo at its source, by ignoring RX bytes that arrive while
  or immediately after we transmit. That addresses the root cause rather than
  this symptom, removes the noise that makes the bus hard to analyse, and would
  also have prevented the phantom-`0x10` stall fixed in PR #25 from being
  reachable at all.

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
ventilating at 57%. The CS60 evidently does not act on the bare read — it
applies a value when the matching coil is raised — but relying on that is a
thin margin, and it means the panel is advertising a setpoint it does not mean.

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

## 4. Proposed setpoint state machine (panel-side, YAML)

This follows directly from §2: the panel owns the setpoints, so the ESP must be
their persistent, authoritative store, with Home Assistant as a UI on top rather
than the owner. HA being unreachable must not affect what we answer the CS60.

Three requirements, in priority order:

1. **Serve the right value before the CS60 asks.** It polls `0x03` and `0x08`
   about seven times a second, so the mirror must be populated during boot, not
   after the first HA interaction.
2. **Survive a reset** without HA, an automation, or the user being involved.
3. **Command the unit once** on boot so the CS60's copy and ours provably agree,
   rather than repeatedly (each `send_cmd` raises a coil the CS60 must consume).

### Design

```yaml
# Persistent store: the ESP is the authority for these, so they are plain
# optimistic numbers with restore_value, NOT read-backs of our own mirror.
# (The template platform rejects `lambda` together with restore_value /
# optimistic / initial_value, and a read-back would only tell us what we
# already stored.)
number:
  - platform: template
    id: fan_normal
    name: "Fan Speed Normal"
    unit_of_measurement: "%"
    mode: box
    min_value: 1
    max_value: 100
    step: 1
    optimistic: true
    restore_value: true
    set_action:
      - lambda: |-
          id(server)->send_cmd(flexit_modbus_server::REG_CMD_PERCENTAGE_SUPPLY_FAN_NORMAL, (uint16_t)x);
          id(server)->send_cmd(flexit_modbus_server::REG_CMD_PERCENTAGE_EXTRACT_FAN_NORMAL, (uint16_t)x);
  # ... same shape for fan_min (0x02 / 0x07) and fan_max (0x04 / 0x09)

esphome:
  on_boot:
    # After every component is set up, so the server and the numbers exist.
    - priority: -100
      then:
        - lambda: |-
            // Populate the mirror WITHOUT raising coils: the CS60 polls these
            // registers continuously and must never be told the setpoint is 0.
            // write_holding_register() sets the value only; send_cmd() would
            // also raise the coil, which is a command.
            struct { flexit_modbus_server::HoldingRegisterIndex supply, extract;
                     esphome::number::Number *num; } map[] = {
              {flexit_modbus_server::REG_CMD_PERCENTAGE_SUPPLY_FAN_MIN,
               flexit_modbus_server::REG_CMD_PERCENTAGE_EXTRACT_FAN_MIN,    id(fan_min)},
              {flexit_modbus_server::REG_CMD_PERCENTAGE_SUPPLY_FAN_NORMAL,
               flexit_modbus_server::REG_CMD_PERCENTAGE_EXTRACT_FAN_NORMAL, id(fan_normal)},
              {flexit_modbus_server::REG_CMD_PERCENTAGE_SUPPLY_FAN_MAX,
               flexit_modbus_server::REG_CMD_PERCENTAGE_EXTRACT_FAN_MAX,    id(fan_max)},
            };
            for (auto &m : map) {
              float v = m.num->state;
              if (std::isnan(v) || v < 1.0f) continue;   // nothing stored yet
              id(server)->write_holding_register(m.supply,  (uint16_t)v);
              id(server)->write_holding_register(m.extract, (uint16_t)v);
            }
        # Let the CS60 settle before commanding anything.
        - delay: 15s
        - lambda: |-
            // One command per setpoint, so the unit's copy provably matches
            // ours. The CS60 acknowledges via function 0x65 and clears the coil.
            ...same table, but id(server)->send_cmd(...) instead
```

Actual fan output stays a **sensor** (`REG_PERCENTAGE_SUPPLY_FAN`, `0x00CA`).
Keeping setpoint and measurement as separate entity types is what makes the
"reads 0 after a reboot" confusion impossible: numbers are what we intend,
sensors are what the unit reports.

### Notes and caveats

- **Untested.** This design follows from the captures but has not been flashed
  or verified on hardware. The `on_boot` priority, and whether 15 s is the right
  settling delay, both need checking against a real reboot.
- **Divergence detection** is a possible extension: the CS60 repeats its `0x65`
  acknowledgement continuously, so a mismatch between the acknowledged value and
  the stored one could trigger a re-assert. Deliberately left out of the first
  version — it needs a way to observe `0x65` traffic from YAML, which the
  component does not currently expose.
- **Compensation is the unit's own behaviour.** On this installation an external
  hardware switch (kitchen hood) makes the CS60 hold supply at normal and drop
  extract to minimum. We write supply and extract to the same value and let the
  CS60 apply compensation itself; the state machine must not try to mirror or
  fight it.
- **`getCoil` is not exposed** on `FlexitModbusServer`, though
  `ModbusRTUServer::getCoil` exists. Exposing it would allow a "command not yet
  acknowledged" diagnostic, since the CS60 clearing a coil is its acknowledgement.

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
