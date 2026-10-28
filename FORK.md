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

Status as of 2026-10-28, with upstream `main` merged at `67eb792` (the CI600
panel support, PR #39).

| Change | Files | Proposed upstream? |
|---|---|---|
| [Echoed exception responses are not answered](#echoed-exception-responses-are-not-answered) | component, `tests/server_test.cpp` | Not yet |
| [The packages load the component from this fork](#the-packages-load-the-component-from-this-fork) | `packages/core.yaml` | No, it names this fork |

Nothing else in `packages/`, `example.yaml` or the README is changed.
[`flexit-modbus.yaml`](flexit-modbus.yaml) is the configuration of the
installation behind this fork, built on the packages of `live`.
[`flexit-test.yaml`](flexit-test.yaml) is the standalone configuration it
replaces, kept as a fallback.

Earlier differences that are gone, because upstream has them now:

- **`PRIu32` for the baud rate log.** Upstream PR #36.
- **Flash persistence can be turned off.** `persist_settings` and
  `persist_runtime_counters` came in upstream PR #33, and the README describes
  them under "Saved settings and counters".
- **The library link.** Upstream's `modbus_library_ref` option picks the
  ESP-ModbusRTUServer branch, tag or commit, and the README describes it. The
  library's `main` has the RS485 echo filter. Its `dev` branch was deleted after
  library PR #9, so a configuration that still names `dev` no longer builds.

## Echoed exception responses are not answered

Some RS485 transceivers echo the ESP's own transmissions back into RX. An
exception response sent by the component then arrives as an incoming frame, is
answered with another exception, and that answer is byte-identical to the frame
that caused it. The loop never ends: 2748 such frames were counted in a
60 second capture, about as many as the CS60's own requests.

The library's echo filter (on by default) now drops a complete echo before the
frame splitter sees it, which stops that capture's loop at the source. Upstream's
splitter still accepts a 5-byte exception frame and hands it on, and the reply
to an unknown function is still `sendException(data[1])`, so one echo that gets
past the filter would loop the same way. The filter misses an echo when it
arrives more than 50 ms late, or when more than four replies are waiting for
theirs.

So a frame with bit 7 set in the function code, which is a response and never a
request, is passed to the frame callbacks (the TCP bridge, the panel) but not
dispatched, and `on_fc65_` masks bit 7 before replying. The test "An exception
of ours that the echo filter missed comes back" in `tests/server_test.cpp` fails
without these two lines. Boards that do not echo are unaffected.

## The packages load the component from this fork

Upstream's `packages/core.yaml` loads the component from
`MSkjel/esphome-flexit-modbus-server`, so packages taken from this fork would
still come with upstream's component. Here `core.yaml` loads it from this fork,
at `flexit_ref`, which defaults to `live`. Packages and component then come from
the same branch, and so does the `pbus.c` that both the component and the panel
packages build.

The component's branch comes from `flexit_ref` alone, so the packages' `ref:`
should be `${flexit_ref}` too, as below.

## Using this branch

With the packages, as `example.yaml` shows them, from this fork:

```yaml
substitutions:
  flexit_ref: live
  flexit_refresh: 1d

packages:
  flexit:
    url: https://github.com/peetervalk/esphome-flexit-modbus-server
    ref: ${flexit_ref}
    refresh: ${flexit_refresh}
    files:
      - packages/core.yaml
      # ...
```

Or the component alone, in a standalone configuration like `flexit-test.yaml`:

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/peetervalk/esphome-flexit-modbus-server
      ref: live
    components: [flexit_modbus_server]
```

The component needs the whole repository, not just its folder: it builds
`packages/code/panel/pbus.c`. A git source fetches the whole repository, so
this works as shown. To add the panel packages to a standalone configuration,
take them from this branch as well, so they and the component use the same copy
of `pbus.c`.

## Keeping up with upstream

- `main` is synced with upstream and then merged into `live`. `live` is never
  rebased, because installations build from it.
- `git diff main live` shows everything this fork changes.
- An upstream change next to `flexit_ref` or `external_components` in
  `packages/core.yaml` merges with a conflict. Keep this fork's two lines.
- Changes meant for upstream go on their own branches cut from `main`.
