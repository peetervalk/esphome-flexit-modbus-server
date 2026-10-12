# Differences from upstream

This is the `live` branch of
[peetervalk/esphome-flexit-modbus-server](https://github.com/peetervalk/esphome-flexit-modbus-server),
a fork of
[MSkjel/esphome-flexit-modbus-server](https://github.com/MSkjel/esphome-flexit-modbus-server).

`live` is upstream `main` plus the changes on this page, and it is the branch a
running installation builds from. `main` and `dev` in this fork mirror upstream
unchanged. The upstream [README](README.md) applies as written, except where
this page says otherwise.

## Summary

Status as of 2026-10-12.

| Change | Files | Proposed upstream? |
|---|---|---|
| [Flash persistence can be turned off](#flash-persistence-can-be-turned-off) | component | No |
| [Echoed exception responses are not answered](#echoed-exception-responses-are-not-answered) | component | Not yet |
| [`PRIu32` for the baud rate log](#priu32-for-the-baud-rate-log) | component | Not yet |
| [ModbusRTUServer comes from a fork](#modbusrtuserver-comes-from-a-fork) | `__init__.py` | Library fix: not yet. The link itself stays fork-only. |

Nothing in `packages/`, `example.yaml` or the README is changed.

## Flash persistence can be turned off

Upstream saves the setting registers and the runtime counters to flash and
restores them when the ESP boots (see "Settings read-back" and "Runtime
counters" in the README). Settings are saved whenever one changes, the counters
once an hour. A setpoint that an automation adjusts every few minutes is
therefore written to flash every time it changes.

Two options control this. Both default to `true`, which is upstream's behaviour.

```yaml
flexit_modbus_server:
  - id: server
    uart_id: modbus_uart
    address: 1
    persist_settings: false           # setting registers
    persist_runtime_counters: false   # runtime counters
```

| Option | When `false` |
|---|---|
| `persist_settings` | The setting registers are not restored at boot and never saved. After an ESP restart they read 0 until the CS60 sends them or the configuration writes them, as before upstream PR #27. |
| `persist_runtime_counters` | The counters are not restored at boot and never saved. After an ESP restart an hour counter reads unknown until the CS60 has sent both of its words; the high word can take about 18 hours. |

The boot log shows both under `Flexit Modbus Server:`.

Things to know when turning them off:

- **The configuration owns the boot values.** Whatever should be served to the
  CS60 after a restart has to be written by the YAML, for example template
  numbers with `restore_value` and an `on_boot` block.
- **The packages assume persistence is on.** With `persist_settings: false`
  their settings read 0 or show as unknown after an ESP restart, until the CS60
  sends them again or they are set.
- **Old data is not erased.** Turning an option back on restores whatever was
  saved before it was turned off.

## Echoed exception responses are not answered

Some RS485 transceivers echo the ESP's own transmissions back into RX. An
exception response sent by the component then arrives as an incoming frame, is
answered with another exception, and that answer is byte-identical to the frame
that caused it. The loop never ends: 2748 such frames were counted in a
60 second capture, about as many as the CS60's own requests.

A frame with bit 7 set in the function code is a response, never a request, so
the frame splitter now consumes it without dispatching it. `onInvalidFunction`
also masks bit 7 before replying. Boards that do not echo are unaffected.

## `PRIu32` for the baud rate log

`dump_config` printed a `uint32_t` with `%u`, which draws a format warning on
ESP32. It was the only compiler warning in the component.

## ModbusRTUServer comes from a fork

`__init__.py` fetches the library from
[peetervalk/ESP-ModbusRTUServer](https://github.com/peetervalk/ESP-ModbusRTUServer)
at commit `e7884ad` (branch `fix/exception-tx-enable`) instead of
`MSkjel/ESP-ModbusRTUServer`.

The one difference: `sendException` goes through `sendResponse`, so TX enable is
asserted and the stream is flushed, as for every other reply.

- **Boards with a `tx_enable_pin`:** upstream's library never puts an exception
  on the bus, because the driver is not enabled for it. With this fork
  exceptions are transmitted. This has not been tested on such a board; the
  installation behind this fork has no `tx_enable_pin`.
- **Updating the library:** change the commit hash in `__init__.py`. ESPHome
  clones a library once and reuses that clone, so pushing to the branch is not
  enough.
- **Local copies still win:** as upstream, a `ModbusRTUServer.h` or `.cpp` in the
  component folder is used instead of the fetched library.

## Using this branch

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/peetervalk/esphome-flexit-modbus-server
      ref: live
    components: [flexit_modbus_server]
```

`packages/core.yaml` is unchanged and still loads the component from
`MSkjel/esphome-flexit-modbus-server`. A configuration built on the packages
therefore gets upstream's component, not this one. This branch is used with a
standalone configuration.

## Keeping up with upstream

- `main` is synced with upstream and then merged into `live`. `live` is never
  rebased, because installations build from it.
- `git diff main live` shows everything this fork changes.
- Changes meant for upstream go on their own branches cut from `main`, without
  the library link.
