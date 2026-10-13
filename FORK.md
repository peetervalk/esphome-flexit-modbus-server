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

Status as of 2026-10-13, with upstream `main` merged at `591e789`.

| Change | Files | Proposed upstream? |
|---|---|---|
| [Echoed exception responses are not answered](#echoed-exception-responses-are-not-answered) | component | Not yet |

Nothing in `packages/`, `example.yaml` or the README is changed.
[`flexit-test.yaml`](flexit-test.yaml) is the configuration of the installation
behind this fork.

Three earlier differences are gone, because upstream has them now:

- **`PRIu32` for the baud rate log.** Upstream fixed the format warning in
  `dump_config` in PR #36.
- **Flash persistence can be turned off.** `persist_settings` and
  `persist_runtime_counters` were merged in upstream PR #33. They are described
  [below](#the-persistence-options), because the upstream README does not
  mention them yet.
- **The library link.** This branch used to fetch ESP-ModbusRTUServer from a
  fixed commit set in `__init__.py`. Upstream's `modbus_library_ref` option
  replaces that, see [Choosing the library branch](#choosing-the-library-branch).

## Echoed exception responses are not answered

Some RS485 transceivers echo the ESP's own transmissions back into RX. An
exception response sent by the component then arrives as an incoming frame, is
answered with another exception, and that answer is byte-identical to the frame
that caused it. The loop never ends: 2748 such frames were counted in a
60 second capture, about as many as the CS60's own requests.

A frame with bit 7 set in the function code is a response, never a request, so
the frame splitter now consumes it without dispatching it. `onInvalidFunction`
also masks bit 7 before replying. Boards that do not echo are unaffected.

The library's echo filter (see
[Choosing the library branch](#choosing-the-library-branch)) drops a complete
echo before the splitter sees it. This check stays for whatever gets past the
filter, and for builds against a library commit from before it.

## The persistence options

Upstream saves the setting registers and the runtime counters to flash and
restores them when the ESP boots (see "Settings read-back" and "Runtime
counters" in the README). Settings are saved 10 minutes after the last change,
or one hour after the first unsaved one at the latest; the counters once an
hour. A setpoint that an automation keeps adjusting is therefore still written
to flash about once an hour.

Two options control this. Both default to `true`.

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

## Choosing the library branch

The component is built on
[MSkjel/ESP-ModbusRTUServer](https://github.com/MSkjel/ESP-ModbusRTUServer).
Upstream's `modbus_library_ref` option picks the branch, tag or commit to build
against. Without it the library's `main` is used.

```yaml
flexit_modbus_server:
  - id: server
    uart_id: modbus_uart
    address: 1
    modbus_library_ref: dev
```

`flexit-test.yaml` builds against `dev`. On 2026-10-13 `dev` and `main` have
the same code: commit `75e0881` was merged into `main` in library PR #7, so the
option is no longer needed to get it. That commit does two things:

- **Echo filter.** Bytes that match the frame the library just sent are held
  back, and dropped once the whole frame has come back. Anything else is passed
  on untouched. It is on by default.
- **`sendException` goes through `sendResponse`**, so TX enable is asserted and
  the stream is flushed, as for every other reply. This was the one change in
  the peetervalk library fork this branch used before, so that fork is no longer
  needed.

Things to know:

- **Boards with a `tx_enable_pin`:** before `75e0881` the library never put an
  exception on the bus, because the driver was not enabled for it. Now
  exceptions are transmitted. This has not been tested on such a board; the
  installation behind this fork has no `tx_enable_pin`.
- **Updating the library:** ESPHome clones a library once and reuses that clone,
  so a new commit on a branch is not picked up by itself. Use Clean Build Files,
  or set `modbus_library_ref` to the commit hash and change it to update.
- **Local copies still win:** a `ModbusRTUServer.h` or `.cpp` in the component
  folder is used instead of the fetched library, and `modbus_library_ref` is
  then ignored.

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
- Changes meant for upstream go on their own branches cut from `main`.
