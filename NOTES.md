# WildStak technical notes

This document explains how the whole system fits together and, more
importantly, *why* the firmware looks the way it does. Several parts of
`firmware/wildstak.yaml` and `firmware/components/waveshare_epaper/` diverge
from what Seeed's own documentation says to do, because that documentation is
wrong for this specific hardware in ways that took real debugging to find. If
you're modifying this project (human or LLM), read the "Hardware quirks"
section before touching the display driver or `online_image` config: it'll
save you from re-discovering bugs that are already fixed here.

The measurements below come from one unit on the bench, in September 2026.

## System overview

```
[HA sensors] --> dashboards/*_dashboard.py --> docs/media/*.png --+
                                                                   +--> docs/ on any web server
                 make --> docs/data/dashboards.txt (manifest) ----+            |
                                                                               v
                            Home Assistant <-- MQTT --> reTerminal E1001 (firmware/)
```

1. `dashboards/weekly_energy_dashboard.py` and `dashboards/daily_energy_dashboard.py` pull data from Home Assistant's REST API and render 800×480 1-bit PNGs using matplotlib/Polars/PIL, thresholded to pure black/white. Both are thin card scripts built on the shared `dashboards/dashboardlib.py` library and `dashboards/config.toml`; see `DASHBOARD_SPEC.md` for the config/CLI contract and design principles if you're adding a new dashboard.
2. `make`, run from cron (hourly, say), runs every card script, writes the PNGs to `docs/media/`, and writes the **manifest** `docs/data/dashboards.txt`: when the cards were generated, and a content hash and URL per card (see "Manifest format"). Any static web server can serve `docs/`; `make site` also builds a Zensical site around it.
3. The reTerminal E1001 wakes on a schedule, syncs when one is due (manifest, then only the changed cards, into a flash cache), and shows the cards in rotation with partial refreshes, deep sleeping in between. Settings come from Home Assistant and readings go back to it, over MQTT.

Adding a new dashboard: see `DASHBOARD_SPEC.md`. In short: drop a script in `dashboards/`, add a `[cards.<name>]` table to `config.toml`, run `make`. No firmware rebuild.

## Firmware (v0.3)

Designed on 2026-09-28 for ESPHome 2026.9.0 and built on 2026-09-28 and 29.
It replaced v0.2, a task-queue design under which deep sleep was unreliable;
"History: v0.2" at the end explains what went wrong. The control logic is in
`firmware/wake.h`, with `card_cache.h` and `dns_cache.h`; `wildstak.yaml`
wires up the hardware and the network steps.

### Principles

1. **One wake is one pass through the loop.** ESPHome has no light sleep
   ([feature request #141](https://github.com/esphome/feature-requests/issues/141),
   open since 2019), so the chip reboots on every wake. Rather than
   integrating sleep into a long-running event loop, each boot runs one
   straight-line pass and then sleeps until the next thing is due. This
   is the pattern used by TRMNL and by both public ESPHome firmwares for
   this device (see "Prior art" below).
2. **The schedule is a pure function of the clock.** What to show and
   whether a sync is due are computed from the PCF8563's wall-clock time,
   the current settings, and what is cached. No queue, no pending flags,
   no saved "next action".
3. **Rotation never touches the radio.** Cards are cached in flash, so
   a rotation wake is boot, draw, partial refresh, sleep: about 1 s.
   Only sync wakes turn on Wi-Fi.
4. **The device starts every conversation.** Home Assistant is a mailbox,
   not a peer: settings are retained MQTT messages the device reads when
   it wakes; readings are retained messages it publishes. Nothing waits
   for Home Assistant to notice the device.
5. **Sleeping and staying awake share one code path.** With deep sleep
   off, the device runs the same pass and then *waits* for the next
   deadline instead of sleeping until it.

### Modes and the wake cycle

Settings start as substitutions in `wildstak.yaml`; Home Assistant overrides
them over MQTT (see "Home Assistant interface").

Two modes:

- **Carousel** (cards only). Each wake draws the card the clock calls for
  (radio off, so the refresh light-sleeps and the card appears on time),
  then syncs if one is due, then redraws only if the sync rewrote the card
  on the panel (the previous frame is still valid in RAM). With no valid
  time or an empty cache it syncs first. White buttons: next / previous
  (shift `manual_offset`); a button wake stays awake 30 s for more presses.
- **Service mode.** Green button, or any power-on / reflash (replaces the
  old fixed OTA window). Wi-Fi up, sync immediately, then a live status
  screen (sync result, clock, battery, temperature, network, cache, settings)
  with a countdown bar that moves every 5 s (each a partial refresh; a
  per-second bar would mean refreshing nonstop, as the waveform takes
  ~0.9 s). Reachable on the network for `service_s` seconds (default 120;
  HA-configurable). Green again: countdown restarts. White: back
  to the cards now. An OTA upload in progress keeps it awake. On exit the
  radio goes off first, then the carousel card is drawn: a partial refresh,
  because the service screen is still the driver's previous frame in RAM.

Why this shape: the diagnostics card used to be a carousel item, which
needed a frozen snapshot in RTC memory so the next wake could re-render it
as the partial-refresh base. As a mode it only ever transitions within one
awake session, where the driver already holds the previous frame, so that
code is gone. Maintenance mode (green held at wake) is gone too: service
mode is the reachable window.

- **Previous-frame rule** (`ensure_base()`): within an awake session the
  driver's RAM copy is authoritative; after a deep sleep only a card can be
  rebuilt (from flash); anything else gets a full refresh.
- **De-ghosting**: `full_refresh_every` (default 60 refreshes; 0 = never;
  HA setting). No ghosting has been seen on this panel, but Good
  Display's guidance is to full-refresh periodically, so 0 is left as a
  deliberate choice. A scheduled full refresh never lands mid-countdown: in
  service mode it is deferred and done on the way back to the carousel.
- **Triggers.** `on_boot` starts the first pass (no wait for the tick); the
  `interval: 1s` tick starts later passes when a deadline passes or a
  button asks, and calls the sleep gate. Every pass also calls the sleep
  gate itself when it ends. No script calls back into the tick.
- **Sync** measures battery (divider enabled only while measuring, before
  Wi-Fi comes up so radio current doesn't sag the reading) and SHT40, waits for SNTP only when the clock is
  invalid or more than a day old, then manifest and changed cards; Wi-Fi goes
  off again unless service mode or awake mode (`deep_sleep_enabled: false`)
  needs it.
- **Bench (20 s rotation):** carousel cards on the 20 s grid, all partial,
  a full refresh every `full_refresh_every`. Service mode after a serial
  flash: countdown bar partial refreshes every ~5 s, then off the network
  (back to the carousel, deep sleep) when the countdown ended.

### Card position from the clock

```
L     = card count
slot  = floor(now / rotate_s)
index = (slot + manual_offset) mod L
next  = (slot + 1) * rotate_s             # wake at this epoch
```

- `now` is read from the PCF8563 on every wake. The ESP32's deep-sleep
  timer runs off an internal RC oscillator that is off by a few percent,
  so the sleep duration is only a hint. Waking early just means sleeping
  again for the remainder; waking late shows whichever slot it is.
  Nothing accumulates, so nothing drifts.
- Buttons change `manual_offset` (+1/-1), kept in RTC memory. The cycle
  then continues from the chosen card at the next slot boundary.
- Changing `rotate_s` from Home Assistant shifts the phase once. That is
  acceptable; no anchor bookkeeping.
- If the RTC time is invalid (fresh power-up with no backup cell), the
  pass becomes a sync wake to set the time first.
- The PCF8563 is believed to run from the board's 3.3 V rail, which
  stays up during ESP32 deep sleep, so the CR1220 backup cell only
  matters when the main battery is disconnected or flat. **To verify:**
  sleep the device, wake it with Wi-Fi off, and check the time it reads
  before any sync sets it (USB serial log).

Sync scheduling uses the v0.2 period/offset grid unchanged: a sync is due
when `now` has passed the most recent boundary
`floor((now - offset) / period) * period + offset` and the last successful
sync was before that boundary. The device's next wake is
`min(next rotation slot, next sync boundary, longest-sleep cap)`
(`max_sleep_min`).

### Control structure in ESPHome

- **One script, `wake_cycle`, `mode: single`, straight-line.** It never
  calls back into anything that could start it again. Helpers it calls
  (draw, fetch) are leaves.
- **After the first pass, one `interval: 1s` tick is the only thing that
  starts work.** Each
  tick: if `wake_cycle` is running, do nothing. Else if the next deadline
  has passed, or a button/HA redraw was requested, start `wake_cycle`.
  Else, if nothing holds the device awake and the awake grace period has
  expired, `deep_sleep.enter` with `sleep_duration = deadline - now`.
  This is the pattern from mehdisadeghi/reterminal-e1001-esphome.
- `on_boot` starts the first pass (so a rotation wake doesn't wait up to a
  second for the first tick); the tick starts every later one.
- Buttons (as `binary_sensor`s, sharing pins with ext1 via
  `allow_other_uses: true`) only mutate state and set a redraw request.
  Presses in the first second after boot are ignored: the press that woke
  the device also fires `on_press`.
- No `run_duration` on `deep_sleep`. The tick owns sleep, and an OTA
  upload in progress (`ota: on_begin`) keeps it awake.
- Complex logic (schedule math, ext1 decoding, flash cache, RTC state)
  lives in a C++ header pulled in with `esphome: includes:`, called from
  short lambdas. YAML stays as wiring.

### State: where each thing lives

| State | Storage | Why |
|---|---|---|
| `manual_offset`, `shown_index`, driver `at_update_`, last sync epoch, settings in use, wake stats | RTC memory (`RTC_DATA_ATTR` in the C++ header) | Written every wake; survives deep sleep; no flash wear. Lost on power loss, which just forces a sync. |
| Settings received from HA | RTC memory (`state.ha_settings`) | Survive deep sleep. After a power loss the YAML defaults apply until the next sync, which a cold boot does at once. |
| Card bitmaps (1bpp, 48,000 bytes each) + per-card hash + card count | Dedicated flash partition, one fixed slot per card (custom partition table via `esp32: partitions:`) | Survives deep sleep and power loss. Written only when a card's hash changes: ~24 erases/day/sector worst case, over 10 years at 100k cycles. The E1001 has 32 MB flash; `partitions_cards.csv` declares 16 MB and gives the cache 2 MB, 32 slots. |
| Manifest URLs | Re-fetched on sync; not needed on rotation wakes (cards are addressed by slot number) | |
| PSRAM | Scratch only (PNG decode during sync) | Lost in deep sleep. |

RTC slow memory is 8 KB, so it holds state, not images.

### Display: partial refresh across deep sleep

Stock, the driver keeps the previous frame (`old_data_`) and the refresh
counter (`at_update_`) in RAM and puts the panel into its own deep sleep after
every refresh. After an ESP32 deep-sleep wake both would be gone,
`at_update_ == 0`, and the first refresh would be a full (~3.5 s, flashing)
one: at one rotation a minute, a flash every minute and roughly 4x the
rotation-wake energy.

What the vendored driver does about it (`gooddisplay_gdey075t7.{h,cpp}`; the
component's README.md lists every change):
- The firmware seeds the refresh counter from RTC memory
  (`set_update_count()`; fabianwimberger's `epaper_spi` patch does the same).
- The firmware supplies the previous frame: before drawing, it loads the card
  that's on the panel (`shown_index`) from the flash cache into `old_data_`.
  If the previous frame isn't reproducible (state lost, cache changed
  underneath it), the refresh is a full one.
- The periodic de-ghosting full refresh is a setting (`full_refresh_every`).
- Busy waits can light-sleep the ESP32, and setup skips a redundant panel
  reset. Both are measured below.

### Home Assistant interface: MQTT

There's no native API; the Home Assistant link is MQTT, through HA's MQTT
integration and discovery, on whatever broker HA uses (e.g. the Mosquitto
add-on). Plain MQTT by default; TLS is an override in `device.yaml` (see
`firmware/certs/README.md`). The device logs in as its own MQTT user
(`mqtt_username`, password in `secrets.yaml`); if the broker has ACLs, that
user needs the device's `<name>/` topics and the `homeassistant/` discovery
prefix.

- **Settings (HA -> device):** the device's own MQTT controls, on its HA
  device page: numbers `Rotate interval`, `Sync offset`, `Service mode
  time`, `Full refresh every`, select `Sync period` (minutes dividing 60)
  and switch `Deep sleep`. `command_retain: true` makes HA publish each
  change as a retained message, so every setting is its own mailbox at the
  broker. On a sync the device enables MQTT, the broker delivers the
  retained commands right after the subscriptions, and each `set_action`
  calls a setter in `wake.h` (validate, apply, persist), then reports the
  applied value back, so HA shows what the device actually uses. The sync
  waits up to 1 s for all six. Applied settings live in RTC memory
  (`state.ha_settings`) and survive deep sleep; after a power loss the YAML
  defaults apply until the first sync (immediate on a cold boot). The deep
  sleep switch uses `restore_mode: DISABLED`: any other restore mode runs
  turn_on/off_action at boot and silently overwrote the setting. (This
  replaced a first version with HA helpers, an automation and one retained
  JSON message; the controls keep settings and readings on one device page
  and need nothing in HA's configuration.)
- **Readings (device -> HA):** retained, via MQTT discovery: battery (V and
  %), temperature, humidity (published when MQTT connects), plus the last
  sync time (a timestamp), Wi-Fi
  signal, sync duration, last wake's active time, card count, wakes since
  the last power loss (`total_increasing`), last sync
  result and firmware version (published at the end of each sync). No birth,
  will or shutdown message, so entities keep their last value while the
  device sleeps; `expire_after: 3h` marks measurements stale if syncs stop.
- **Logs** go to `<name>/debug` (INFO and up) while connected, so
  `esphome logs device.yaml --device MQTT` works without the API; USB
  serial always works.
- **TLS (optional):** `certificate_authority` can pin ISRG Root X1, X2, YR
  and YE (`firmware/certs/`, with provenance).

Two problems found on the bench:

- **MQTT must not be enabled at boot.** With `enable_on_boot` (the default)
  the client resolves the broker during setup, while Wi-Fi is still off; the
  lookup fails and the client then waits out a 20 s DNS timeout and a 5 s
  retry delay (`mqtt_client.cpp`), longer than a whole sync, so the
  end-of-sync readings were never sent. Now `enable_on_boot: false`,
  `mqtt.enable` right after Wi-Fi connects, `mqtt.disable` before the radio
  goes off. ESPHome's MQTT client resolves with lwIP's `dns_gethostbyname`,
  not `getaddrinfo`, so `dns_cache.h` doesn't cover it.
- **Card count after a reset.** `state.n_cards` is in RTC memory, so a
  power loss or reflash zeroed it although the flash cache still had every
  card; a failed first sync would have shown "No cards yet". `on_boot` now
  counts valid cache slots when the RTC state is reset.

Bench result (cold boot, service-mode sync, no config message yet): MQTT
connected 0.9 s into the sync (DNS 0.14 s + TLS), sync 3.0 s including the
1.5 s wait for the absent config message, all readings in HA. Minimum free
internal heap with the MQTT and HTTPS TLS sessions both open: 156 KB.

Rejected alternatives: the native API (HA initiates the connection, so
every wake waits an unpredictable few seconds for HA to reconnect before
the settings arrive); HA's REST API called from the device (works, but
needs an HA token on the device, and states posted that way do not
survive an HA restart).

### Staying awake, OTA, and recovery

The device stays awake (the tick doesn't put it to sleep) while:
- the `Deep sleep` switch is off (in HA, or `deep_sleep_enabled` in YAML);
- service mode is running (green button, or any power-on / reflash);
- an OTA upload is in progress (`ota: on_begin`);
- within 30 s of a button press.

USB serial always works. With ESPHome 2026.8.0+ entering deep sleep counts as
a successful boot, so OTA updates are no longer rolled back when a device
sleeps early. Safe mode stays enabled as the last-resort recovery (ten failed
boots -> OTA only).

### Manifest format

`docs/data/dashboards.txt`, written by `make`: line 1 is the ISO8601 time the
cards were generated, then one `<hash> <url>` line per card, where `<hash>` is
the first 16 hex chars of the PNG's SHA-256. The file is written to a temp
name and renamed, so a fetch never sees it half-written.

- Card order in the manifest is the rotation order; slot `i` in the flash
  cache is manifest line `i`.
- A content hash is needed because every `make` run rewrites every PNG, so a
  web server's ETag (nginx: `mtime-size`) and Last-Modified change on every
  run regardless of content; HTTP conditional requests can't detect unchanged
  cards.

### Measurements (bench, 2026-09-28)

Taken with bench test firmwares (not in this repo) and the firmware itself.

**Partial refresh across deep sleep.** The driver now
has `get_update_count()`/`set_update_count()`, `capture_previous_frame()`,
`last_refresh_was_full()`, a forced full refresh when no valid previous
frame was supplied, and a guard so `deep_sleep()` is not re-sent to an
already-sleeping panel. With the counter in RTC memory and the previous
frame re-rendered on each wake, 8 of 8 wakes from ESP32 deep sleep used a
partial refresh with no busy timeout. Measured (app time only, excluding
the bootloader): ESPHome setup 277 ms, capture + render + partial refresh
1550 ms, awake 1828 ms per rotation wake. That is about 1.7x the 1.1 s
estimate below; the refresh path (1.55 s against the ~0.34 s the driver
comment claims for the waveform itself) is the place to look for savings.

Per-phase breakdown of one partial-refresh wake (driver `get_timing()`,
identical across wakes to within 0.1 ms):

| Phase | 2 MHz SPI (ms) | 10 MHz SPI (ms) |
|---|---|---|
| capture previous frame (render + copy) | 57.7 | 57.7 |
| render new frame | 52.9 | 52.9 |
| reset pulse | 20.1 | 20.2 |
| power on (100 ms fixed delay + busy) | 128.3 | 128.3 |
| SPI previous frame | 193.7 | 40.1 |
| SPI new frame | 193.1 | 39.6 |
| refresh waveform (0x12 + busy) | 855.3 | 855.5 |
| power off (0x02 + busy) | 41.8 | 41.8 |
| **total refresh / awake** | **1549 / 1826** | **1242 / 1519** |

The partial waveform itself is 855 ms, not ~0.34 s. With 10 MHz SPI,
about 925 ms of each wake is the CPU busy-polling the panel (power-on
tail, waveform, power-off), which makes light-sleeping through those
waits the largest remaining energy lever (TRMNL saw E1001 GPIO-state
trouble with light sleep; see PR #398). Smaller items: overlap the
~110 ms of CPU rendering with the 128 ms power-on ramp, and shorten the
20 ms reset pulse.

**Light sleep during the busy waits works (bench, 2026-09-28).** With
`set_light_sleep_waits(true)` the driver pad-holds RESET/DC/CS
(`gpio_hold_en`) and calls `esp_light_sleep_start()` with a GPIO wake on
BUSY going high plus a timer backstop, instead of busy-polling; the
100 ms power-on delay becomes a timed light sleep. 5 of 5 partial
refreshes were clean (no flash, no ghosting, no timeouts), each wake
spending about 1026 ms in 4 light sleeps. Elapsed time per wake is
unchanged (~1.5 s), but the full-clock CPU time drops from ~1519 ms to
~503 ms (277 ms of it ESPHome setup), so energy per rotation wake roughly
halves. Pad-holding RESET is what avoids the GPIO-glitch problem TRMNL
hit.

Consequences:
- **ESPHome's `millis()` stops during a manual light sleep.** On ESP32 it
  is the FreeRTOS tick count (`components/esp32/hal.cpp`), which does not
  advance in `esp_light_sleep_start()`; `micros()` (esp_timer) does. So
  ESPHome delays, intervals and timeouts stretch by the time slept, and
  `millis()` deltas measure active CPU time only. Wall-clock logic must
  use the RTC (it does, by design), and the driver's own busy-wait timeout
  relies on the light-sleep timer backstop.
- **Only enable it while Wi-Fi is off** (rotation wakes). With the radio
  associated, light sleep would stall it.
- With light sleep, overlapping rendering with the power-on ramp no
  longer saves energy: the ramp is already spent asleep, and what costs
  energy now is active CPU time, not elapsed time. The next lever is the
  ~277 ms of ESPHome setup plus the bootloader/PSRAM-init time before it
  (measured below).

**Boot time from a deep-sleep timer wake (bench, 2026-09-28).** Measured on
the RTC timer, which keeps counting through deep sleep and is the clock the
wake timer runs on: the test records `esp_rtc_get_time_us()` just before
`esp_deep_sleep()`, so (entry + sleep length) is the wake moment, and reads
it again in a C++ static constructor (after bootloader, clock and PSRAM
init) and at `on_boot`.

| Config | wake -> static ctors | ctors -> `on_boot` |
|---|---|---|
| Stock (PSRAM memtest on, ROM log on) | ~730 ms | 278 ms |
| `CONFIG_SPIRAM_MEMTEST: n` | ~300 ms | 278 ms |
| memtest off + `esp_deep_sleep_disable_rom_logging()` | ~300 ms | 278 ms |

- The PSRAM memory test costs ~430 ms on every wake. It is disabled in
  `wildstak.yaml`.
- Quieting the ROM log made no measurable difference.
- `esp_timer_get_time()` reads 1.3 ms at static-ctor time, so app startup
  before the ctors is negligible; the remaining ~300 ms is ROM + second-stage
  bootloader. The bootloader here logs at INFO over 115200 baud
  (`CONFIG_BOOTLOADER_LOG_LEVEL=3`), validates the ~0.8 MB app image on
  every wake (`CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP` not set), and
  reads flash in DIO mode. All three are bootloader settings: OTA does not
  replace the bootloader, so testing them needs a USB serial flash.
- ctors -> `on_boot` (278 ms) is FreeRTOS start plus ESPHome component setup,
  and matches `millis()` at `on_boot`: ESPHome's clock starts at the scheduler.

Per rotation wake with memtest off and light sleep: ~300 ms boot + ~278 ms
setup + ~226 ms of active drawing/refresh work ≈ 0.8 s at full clock, plus
~1.0 s in light sleep.

**ESPHome setup time (bench, 2026-09-28).** Per-component setup times were
captured with `logger: level: VERBOSE` plus an `on_message` trigger that
copies the `component` tag's "Setup X took N ms" lines into RAM (they are
emitted before any API client connects, so they only reach the UART
otherwise; lines >= 50 ms are logged at CONFIG level from a different
source line). Result: **`display` took 234 ms**; every other component took
0-1 ms. Turning UART logging off (`baud_rate: 0`) saved only ~2 ms.

The 234 ms was the base class's setup-time panel reset
(`WaveshareEPaperBase::setup_pins_()` -> `reset_()`: RESET low for
`reset_duration_`, 200 ms default, then 20 ms). It is redundant for this
panel because `init_full_()`/`init_partial_()` reset the controller before
every refresh, so `GDEY075T7` now overrides `setup()` without it (the panel
stays in its own deep sleep until the first refresh). Result: ctors ->
`on_boot` 278 ms -> **59 ms** (display setup 14 ms), refreshes unchanged
and clean. The remaining ~40 ms outside component setups is app/FreeRTOS
start and pre-setup work; not investigated.

Per rotation wake now: ~300 ms ROM + bootloader, ~59 ms setup, ~225 ms
drawing/refresh work ≈ **0.58 s at full clock**, plus ~1.0 s in light
sleep. The bootloader is now over half the active time; see the
bootloader-side options above (needs a USB flash).

**Bootloader settings (bench, 2026-09-28, USB serial flash).** The flash is
a Winbond W25Q256 (`esptool flash-id`: manufacturer `ef`, device `4019`,
32 MB, eFuse flash type quad). Each row was a full serial flash of the same
test firmware; the stock row uses today's IDF 5.5.5 bootloader (the
previously installed one measured ~300 ms).

| Bootloader config | wake -> app start | ESPHome setup | active per wake |
|---|---|---|---|
| Stock (INFO log, validate app, DIO) | ~289 ms | ~61 ms | ~575 ms |
| `CONFIG_BOOTLOADER_LOG_LEVEL_NONE` + `CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP` | ~50 ms | ~61 ms | ~335 ms |
| + `esp32: flash_mode: qio` | **~46 ms** | **~54 ms** | **~323 ms** |

- All three settings are in `wildstak.yaml`. The bootloader ones only take
  effect after a USB serial flash (see the last point).
- With QIO selected, ESP-IDF still writes DIO into the image header and
  `CONFIG_ESPTOOLPY_FLASHMODE="dio"`: the ROM loads the bootloader in DIO
  and the bootloader switches to QIO (`CONFIG_FLASHMODE_QIO=y`). Not a bug.
- Skipping validation applies only to deep-sleep wakes; cold boots and
  post-OTA reboots still verify the app image.
- The bootloader no longer prints anything on serial. To debug boot
  problems, re-enable its log and USB-flash again.
- These are bootloader settings: OTA never replaces the bootloader, so a
  device flashed only over OTA keeps whatever bootloader it had.

**Rotation wake, current totals:** ~46 ms ROM + bootloader, ~54 ms ESPHome
setup, ~222 ms drawing/refresh work: **~0.32 s at full clock**, plus ~1.0 s
in light sleep while the panel refreshes (down from ~2.2 s at full clock
at the start of the day). At this point the unmeasured currents (panel
during refresh, light sleep, board deep sleep) matter as much as the
timings; the next step for the power budget is a real current
measurement.

**Flash card cache.**
`card_cache.h` stores each card as the rendered panel framebuffer (48,000
bytes) plus a header (magic, CRC32, manifest hash) in one 64 KB block of the
`cards` partition (`partitions_cards.csv`: the previous layout unchanged,
NVS still at 0x390000, plus `cards` 2 MB at 0x400000 = 32 slots; flash
declared 16 MB, see the file). The header is written after the frame, so an
interrupted write leaves the slot invalid, not torn. The test ran a cold
sync, rotation wakes, a warm-cache sync, and more rotations, against a
new-format manifest served from the LAN (the cards themselves from
the card server over HTTPS):

| | Result |
|---|---|
| Rotation wake | boot 47 ms, setup 54 ms, flash reads (previous + current frame, CRC-checked) 15 ms, refresh 112 ms active: **~228 ms at full clock** (was ~323 ms with rendering), plus light sleep. All partial, all previous frames restored from flash. |
| Cold sync (empty cache) | Wi-Fi 5.0 s, manifest 25 ms, per card: HTTPS download + PNG decode ~2.5 s, render into framebuffer ~650 ms, flash erase + write ~400 ms. 15.7 s total for 3 cards. |
| Warm sync (nothing changed) | Wi-Fi 5.0 s, manifest 0.6 s, all 3 cards skipped on hash match: **5.6 s total**. |

Re-run against the live server after the Makefile was deployed (OTA, so
the cache persisted across the reboot): the manifest over HTTPS took
~0.53 s. Two cards had been re-rendered with new data and one had not; the
cold-boot sync downloaded and rewrote exactly the two changed cards and
skipped the third on its hash (12.3 s total), and the later warm sync
skipped all three (5.6 s, of which Wi-Fi 5.1 s). Rotation wakes were
identical to the first run.

What this says about sync cost:
- Wi-Fi association + DHCP (5.0 s) dominates a normal hourly sync. Next
  lever: `fast_connect`, a static IP, and pinning BSSID/channel.
- Each changed card costs ~2.5 s, almost all of it the TLS handshake (the
  PNGs are 5-10 KB); keeping one connection open, or serving cards over
  plain HTTP on the LAN, would cut that.
- Rendering the decoded image into the framebuffer with `it.image()` is
  ~650 ms per card (per-pixel drawing); a direct bit copy from the decoded
  1bpp image would be a few ms. Only paid for changed cards.

**Wi-Fi tuning (bench, 2026-09-28).** ESP-IDF event probes timestamp scan
done, association and IP relative to `wifi.enable`; one cold-boot sync and
one warm (after deep sleep) sync per run:

| Config | warm: scan / associated / IP | warm sync total (3 unchanged cards) |
|---|---|---|
| Stock (DHCP, full scan) | 2.8 s / 3.1 s / 5.0 s | 5.6 s |
| `fast_connect: true` | none / 0.39 s / 2.0 s | 2.6 s |
| + `manual_ip` (outside the DHCP range) | none / 0.30 s / 0.30 s | 1.5 s |
| + `dns_cache.h` | none / 0.30 s / 0.30 s | **1.27 s** |

- `fast_connect` stores the last BSSID/channel (NVS by default on ESP32)
  and connects without scanning. The saved entry is keyed to the firmware's
  config hash, so the first connect after flashing new firmware is slow
  (~2.5 s) and later ones are fast.
- DHCP was ~1.6-1.9 s; the static IP removes it. (`wildstak.yaml` uses DHCP
  by default; `device.example.yaml` shows the static IP override.)
- DNS for the card server (a LAN record with TTL 0; the router answered
  other hosts in ~2 ms) took 40-660 ms on the device.
  `dns_cache.h` keeps one name in RTC memory and answers it from lwIP's
  external-resolve hook (`CONFIG_LWIP_HOOK_NETCONN_EXT_RESOLVE_CUSTOM`), so
  every `getaddrinfo()` including http_request's and online_image's skips
  DNS; TLS still verifies the hostname. It is invalidated by any connection
  failure and by power loss. But the saving was only ~0.24 s: the manifest
  request then took ~0.94 s instead of ~0.5 s, so the delay is in the first
  network exchange after connecting, not in DNS itself. Not pursued further
  (would need a packet capture).
- `power_save_mode: NONE` gave no consistent gain (DNS 43/664 ms against
  446/600 ms with the default LIGHT; manifest unchanged), so the default stays.
- The remaining ~0.5 s manifest time is the HTTPS request itself, mostly
  the TLS handshake.
- A sync wake used to refresh with Wi-Fi still on, so without light sleep
  (1.1 s at full clock). In the built firmware every wake draws its card
  with the radio off, before any sync.

**Settings the firmware depends on** (all bench-verified 2026-09-28): the patched
GDEY075T7 driver (partial refresh across sleep, light-sleep waits, no setup
reset); `data_rate: 10MHz`; `CONFIG_SPIRAM_MEMTEST: n`;
`CONFIG_BOOTLOADER_LOG_LEVEL_NONE: y` and
`CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP: y` (bootloader: USB flash);
`flash_mode: qio`; `flash_size: 16MB` with `partitions_cards.csv`;
`card_cache.h`; `wifi: fast_connect: true` (a static IP is optional, in `device.yaml`);
`dns_cache.h` with `CONFIG_LWIP_HOOK_NETCONN_EXT_RESOLVE_CUSTOM: y`.

ESPHome 2026.9.0 ships an official `epaper_spi` model,
`Seeed-reTerminal-E1001`, with non-blocking refresh and `is_idle()`. It
would need the same across-sleep patch, so the firmware keeps the vendored,
known-good driver instead. It compiles on 2026.9.0.

### Power budget (estimates, not measurements)

Assumptions: ~47 mA awake with radio off at 240 MHz, ~110 mA averaged
with Wi-Fi on, ~50 uA board-level deep sleep (unknown for the E1001), 2000
mAh battery with ~1700 mAh usable.

| Wake | Time | Charge |
|---|---|---|
| Rotation (fast boot, flash cache, partial refresh, no radio) | ~1.1 s | ~0.014 mAh |
| Rotation with the stock driver (full refresh every wake) | ~5 s | ~0.065 mAh |
| Sync (static IP, MQTT, TLS manifest, ~2 changed cards) | ~7 s | ~0.19 mAh |

| Scenario | mAh/day | Battery life |
|---|---|---|
| Rotate 60 s, sync hourly | ~28 | ~60 days |
| Rotate 30 s, sync hourly | ~50 | ~34 days |
| Rotate 60 s, full refresh every wake | ~100 | ~17 days |
| Every rotation a full network wake | ~400 | ~4 days |
| Always awake | ~1100 | ~1.5 days |

At these rotation rates the rotation wake is nearly the whole budget:
every 0.1 s shaved off it saves about 7%. Boot-time tuning via
`esp32: framework: sdkconfig_options:`:
- `CONFIG_BOOTLOADER_SKIP_VALIDATE_IN_DEEP_SLEEP: y` (skip image
  verification on deep-sleep wakes);
- `CONFIG_SPIRAM_MEMTEST: n` (the 8 MB PSRAM test runs every boot);
- quieter log level on battery builds.

All of these are applied now; see "Measurements".

Syncing more often than the server regenerates cards (hourly, per the
Makefile cron) spends energy for nothing. Sync wakes get cheaper with a
static IP and `fast_connect`, and by skipping unchanged cards.

To replace the estimates with numbers: record `millis()` just before each
deep sleep into RTC memory and publish it on the next sync.

### Open questions

- Does the PCF8563 keep time through ESP32 deep sleep without the CR1220
  cell? (Expected yes; see "Card position from the clock".)
- Real rotation-wake and board-level deep-sleep current: the power budget
  above is still estimates.

### Ideas

Service mode ideas for later (discussed 2026-09-29): Wi-Fi setup for other
people (`captive_portal` + `wifi: ap:` or `esp32_improv`, plus a small
`web_server` page for the manifest URL and MQTT broker; DHCP is now the
default); a QR code on the service screen (`qr_code`); an error history
(last few sync failures, RTC memory). Device-side low-battery protection
(stop syncs and refreshes below a critical voltage, one "charge me"
screen) and quiet hours (no rotation overnight) are the most useful next
features.

**Status indicators, drawn server-side (planned, decided 2026-09-29).**
Indicators such as a low-battery icon are drawn onto the
card PNGs on the server by an image post-processor that the Makefile runs
after rendering, using the latest values in Home Assistant (the device
reports battery on every sync). The device needs nothing: an
indicator is ordinary card pixels, so the manifest hash changes when it
appears and the flash cache and partial refreshes just work. (Doing it on
the device would have required recording overlays in the panel state and
redrawing them onto the restored previous frame.) Icon spec: upper-left
corner, a battery outline with a small bump on the right, left 20% filled
black, rest white; shown at <= 20%, cleared at >= 25% to avoid flicker.

### Prior art

- [TRMNL firmware](https://github.com/usetrmnl/trmnl-firmware): wake,
  ask the server what to show and how long to sleep, draw, sleep (~10.5 s,
  ~0.19 mAh per wake). Officially supports the E1001
  ([Seeed wiki](https://wiki.seeedstudio.com/reterminal_e10xx_trmnl/)).
- [fabianwimberger/reterminal-e1001-weather](https://github.com/fabianwimberger/reterminal-e1001-weather):
  ESPHome + MQTT retained, 9 of 10 wakes skip Wi-Fi, patched `epaper_spi`
  refresh counter, blank birth/will.
- [mehdisadeghi/reterminal-e1001-esphome](https://github.com/mehdisadeghi/reterminal-e1001-esphome):
  `RTC_DATA_ATTR` state, ext1 wake on all three buttons with wake-cause
  decoding, `interval: 1s` sleep gate, flash-partition snapshots.
- [Tatham Oddie on deep sleep + OTA](https://tatham.blog/2021/02/06/esphome-batteries-deep-sleep-and-over-the-air-updates/)
  and [Wireworld's sleep modes post](https://www.wirewd.com/make/blog/esphome_sleep_modes/):
  stay-awake flags that live off the device.

## Hardware quirks (read this before touching the display driver)

This is the expensive part. None of it is documented by Seeed in a way
that would let you find it without a logic-analyzer-free, log-driven
debugging session.

### The panel is not what Seeed's ESPHome docs say it is

Seeed's [ESPHome cookbook for the reTerminal E Series](https://wiki.seeedstudio.com/reterminal_e10xx_with_esphome/)
says to use ESPHome's built-in `waveshare_epaper` component with
`model: 7.50inv2` (or `7.50inv2alt`). **All three built-in model variants
we tried (`7.50inv2`, `7.50inv2alt`, `7.50inv2p`) produced corrupted
output** — solid black, reproducible "snow" (deterministic per-pixel
scrambling, not noise), or a busy-pin timeout — despite the pins matching
Seeed's docs exactly.

The actual panel is a **Good Display GDEY075T7** (UC8179 controller), not
a genuine Waveshare panel. The proof is in Seeed's own hardware repo, not
their ESPHome docs: their [GxEPD2 Arduino reference example](https://github.com/Seeed-Projects/OSHW-reTerminal-Series-E-D/blob/main/examples/base/GxEPD2_reTerminal_E1001/GxEPD2_reTerminal_E1001.ino)
explicitly instantiates `GxEPD2_750_GDEY075T7`. UC8179 is the same
controller family genuine Waveshare V2 panels use, which is presumably
why ESPHome's Waveshare-oriented classes get *close* (busy-pin handshake
completes, something gets drawn) without being correct.

The fix: [parkghost/esphome-epaper](https://github.com/parkghost/esphome-epaper)
provides a `gdey075t7` model built for this exact panel. It's **vendored
locally** in `firmware/components/waveshare_epaper/` rather than pulled
live via `external_components: source: github://...`, because it carries
local changes (the polarity fix below, plus the deep-sleep work; that
directory's README.md lists them) that a live pull would silently overwrite
on the next cache clear.

### Busy-pin double inversion

Seeed's docs say `busy_pin: inverted: true`. That's correct for
ESPHome's *built-in* `waveshare_epaper` component, which doesn't invert
busy-pin polarity itself. The vendored `GDEY075T7` class does
(`is_busy_pin_inverted_() override { return true; }` in
`gooddisplay_gdey075t7.h`). Stacking both inversions cancels out to "not
inverted", so the busy-wait loop polls for the wrong transition and
**always times out** (`Timeout while displaying image!`). Fix: set
`busy_pin: inverted: false` when using this vendored component.

### Color polarity

The vendored driver's `write_frame_()`/`write_partial_()` originally
inverted the framebuffer before sending it (`~this->buffer_[i]`, plus a
hardware polarity register set to invert again for partial refreshes),
based on an assumption that this panel's bit convention is "1 = white"
and needs flipping to "1 = black" for the wire protocol. That assumption
was backwards for this unit. Confirmed empirically with a deliberate
isolation test: draw a black square at `(0,0)` with everything else
white, and see where/what color it comes back as. Correct geometry +
inverted color → pure polarity bug, not a driver/model mismatch. The fix
removes the inversion entirely (both the software `~` in `write_frame_`
and the hardware DDX polarity bit in `write_partial_`) — the panel wants
the framebuffer sent exactly as ESPHome's core produces it.

If you ever swap in a different physical panel revision, this is the
first thing to re-verify: draw a small asymmetric shape in a known
position/color and see if position and color both come back correct.

### `online_image` + `GRAYSCALE` is a real ESPHome bug for monochrome targets

`online_image`'s `GRAYSCALE` decode path itself is fine, but drawing a
`GRAYSCALE`-typed `Image` via `it.image()` goes through
[`image.cpp`](https://github.com/esphome/esphome/blob/dev/esphome/components/image/image.cpp)'s
`IMAGE_TYPE_GRAYSCALE` case, which builds
`Color(gray, gray, gray, 0xFF)` — the 4th ("white"/alpha) channel is
**hardcoded to `0xFF` regardless of `gray`**. `Color::is_on()` is just
`raw_32 != 0`, so it's `true` for *every* pixel, light or dark. The
monochrome driver's pixel-set logic branches purely on `is_on()`, so the
whole image renders as one uniform color regardless of actual content.
Confirmed empirically: a real dashboard PNG rendered solid white.

Fix: use `type: BINARY` instead of `GRAYSCALE` in the `online_image:`
config. Its `IMAGE_TYPE_BINARY` decode path uses a real luminance
threshold (`is_color_on()`, `0.25R + 0.5G + 0.25B` approximation) into a
genuine 1-bit-packed buffer, and the draw path uses explicit
`color_on`/`color_off` rather than `is_on()`. This is also dramatically
faster than the pixel-by-pixel `get_pixel()` + `draw_pixel_at()` lambda
workaround we tried first — that's not needed at all with `BINARY`.

### Cache images in PSRAM explicitly, or crash

*From v0.2, which cached cards in RAM. v0.3 keeps them in flash, but the
allocator rule holds for any large buffer.*

`image_cache` (the prefetched-image store) must use ESPHome's
`RAMAllocator<uint8_t>` (from `esphome/core/helpers.h`), **not** a plain
`std::vector<uint8_t>`. The plain allocator draws from the ESP32-S3's
small internal DRAM heap (~327 KB total, most already spoken for), not
the 8 MB of PSRAM `online_image` itself uses via the same allocator.
With two ~48 KB cached images plus everything else already on the heap,
internal DRAM ran out, `std::vector::assign()`'s internal `operator new`
threw `bad_alloc`, and since this ESP-IDF/ESPHome build doesn't support
real C++ exception unwinding, that became an immediate `abort()` — a
clean crash-reboot loop, confirmed via a symbolized backtrace
(`xtensa-esp32s3-elf-addr2line` against the build's `firmware.elf`)
pointing at `__wrap___cxa_allocate_exception`.

Declare it as:
```yaml
- id: image_cache
  type: std::vector<std::vector<uint8_t, esphome::RAMAllocator<uint8_t>>>
```

### The event-loop race condition in sequential prefetch

*From v0.2. v0.3 starts each download from the sync script and waits for it,
so it never calls `set_url` from inside the callback.*

`online_image`'s `on_download_finished` callback fires synchronously from
inside `OnlineImage::loop()`, **before** that same `loop()` call reaches
`end_connection_()`, which is what resets the internal `decoder_` pointer
that `update()` checks (`if (this->decoder_) { "already being updated"; return; }`).
If `on_download_finished` immediately calls `online_image.set_url` again
(as sequential prefetch needs to, to move to the next manifest URL), it
hits that guard and silently no-ops — the whole prefetch chain stalls
with no error, because nothing schedules another attempt. Fix: a short
`delay: 50ms` before the next `set_url`, which yields control back to the
scheduler long enough for the current `loop()` call to finish and reset
`decoder_` first. See `online_image.cpp`'s `loop()` and `end_connection_()`
in the installed ESPHome package for the exact ordering.

### Full vs. partial refresh

The vendored driver does a full, flashing refresh (best de-ghosting, ~3.5 s+,
all the visible flicker) every `full_refresh_every` refreshes (default 60;
the `Full refresh every` control in HA; 0 = never) and a partial refresh (no
flicker; the waveform measured ~0.86 s) the rest of the time. The counter
survives deep sleep and counts every refresh, service-screen updates
included, so it's not predictable from which card is showing; a full refresh
that comes due in service mode waits for the way back to the cards. If a
redraw looks unexpectedly slow and flashy, it's very likely this periodic
de-ghosting, not a new bug: the driver logs `display() #N/M: FULL` or
`partial` under the `gdey075t7` tag (debug level).

## Reference links

- Seeed reTerminal E-series ESPHome cookbooks (the *shape* of the config is right; the display model/pins section is not, for this exact panel): [Display Basics](https://wiki.seeedstudio.com/reterminal_e10xx_with_esphome/), [I/O, Battery & Power](https://wiki.seeedstudio.com/reterminal_e10xx_with_esphome_advanced/)
- Seeed's hardware reference repo, incl. the GxEPD2 example that reveals the real panel identity: [Seeed-Projects/OSHW-reTerminal-Series-E-D](https://github.com/Seeed-Projects/OSHW-reTerminal-Series-E-D)
- The community driver this project vendors and patches: [parkghost/esphome-epaper](https://github.com/parkghost/esphome-epaper)
- ESPHome components referenced above: [`online_image`](https://esphome.io/components/online_image/), [`waveshare_epaper`](https://esphome.io/components/display/waveshare_epaper/), [`image`](https://esphome.io/components/image/) (the core bug is in this component's `image.cpp`)
- Good Display GDEY075T7 product page: https://www.good-display.com/product/396.html

## History: v0.2

v0.2 was the firmware before v0.3. It isn't in this repo, but its design, and
why it was replaced, explain much of v0.3's shape.

**How it worked.** The control flow is an explicit task queue: five named
tasks (`task_boot`, `task_update_manifest`, `task_download_cards`,
`task_display_card`, `task_sleep`), a single `task_dispatcher` that's the
only thing that ever starts one, and an `idle_scheduler` that's the only
place "what's due right now" is decided. This replaced an earlier design
(v0.1) built around a scattered mix of
script-to-script continuations (`after_manifest_action`/
`after_refresh_action` globals), a bare `is_busy` boolean, and an
always-on `interval: 20s` poll — which together made it possible for two
independent triggers (that poll, and `on_boot`'s own startup chain) to act
on shared state at the same time. Concretely, that race let the
`deep_sleep:` block's own automatic `run_duration` safety-net timer fire
mid-fetch after a real sleep-wake, producing a real, reproduced bug:
enabling "Deep Sleep Enabled" occasionally rendered the diagnostics screen
before Wi-Fi had connected or any sensor had reported (NaN temp/humidity,
blank IP/SSID, no cards).

### The task queue

Every task follows the same shape: check its own preconditions first
(most concretely, never proceed into a fetch with Wi-Fi still
associating — `task_update_manifest`/`task_download_cards` each
`wait_until wifi.connected` with a bounded timeout and bail cleanly if it
never resolves), do its work, then hand off by setting a `pending_*`
global and calling `task_dispatcher` — never by leaving a continuation for
someone else to interpret later.

- **`task_boot`** — hardware init only (battery GPIO, `pcf8563.read_time`,
  `deep_sleep.prevent`), no content-related branching. It does not need
  to know fresh-boot vs. sleep-wake; it just hands off to
  `idle_scheduler`, the same as everything else that finds nothing else
  pending.
- **`idle_scheduler`** — pure local computation, no network. Decides
  manifest staleness by comparing the manifest's own parsed timestamp
  (`cards_generated_at_epoch`) against the Data Update Period/Offset grid
  boundary and the current time — not a separately-tracked checkpoint, so
  there's nothing that can drift out of sync with reality. Also checks
  whether `image_cache` matches `dashboard_urls` (always false right
  after a real sleep, since PSRAM loses power) and whether Rotate
  Interval is due. If nothing's due, it computes how long until something
  will be and either enqueues `task_sleep` for that duration (if "Deep
  Sleep Enabled" and not "OTA Mode") or arms a one-shot delayed
  re-check (`idle_wait`) — this replaces the old `interval: 20s` poll
  entirely; there is no independent always-on trigger left to race
  anything. When the rotate-due check fires, it resets
  `elapsed_since_rotate` to `0` right there, before enqueueing the
  rotate — this accumulator only ever counts up otherwise, so skipping
  the reset here means the very next pass finds the threshold still
  exceeded and re-triggers immediately, forever (confirmed on real
  hardware: a tight rotate loop, one redraw every couple of seconds,
  that also starves the sleep decision below it in priority — "Deep
  Sleep Enabled" never gets reached at all).
- **`task_update_manifest`** / **`task_download_cards`** — same
  HTTP-fetch/prefetch mechanics as before, just re-homed as task bodies.
  A fetch failure doesn't retry immediately (which would starve the sleep
  branch during an outage) — `manifest_attempted_this_wake` caps retries
  to roughly once per Wake Interval heartbeat.
- **`task_display_card`** — draws either the next real card or, once per
  lap through the real cards (`diagnostics_turn_next`), a freshly
  synthesized diagnostics screen. Diagnostics is no longer card `0` of an
  `n+1`-slot rotation — `page_index` indexes only real fetched cards
  (`0..n-1`); diagnostics content is never cached or given an index at
  all, it's recomputed live every time its turn comes up, at the same
  cadence a viewer would have seen it before.
- **`task_sleep`** — the *only* place that ever calls `deep_sleep.allow`,
  called immediately before its own `deep_sleep.enter`. See "Deep sleep
  invariant" below. Before that, it waits out the "Sleep Delay" number
  entity (default 15s, on top of the fixed 2s SPI-safety delay) with
  Wi-Fi/API/OTA still fully up, then re-checks "Deep Sleep Enabled"/"OTA
  Mode" — if either changed during that window, it aborts and hands back
  to `task_dispatcher` instead of committing to sleep. With "Deep Sleep
  Enabled" on, a wake where nothing else is due otherwise only stays
  reachable for well under a second, which isn't enough time to reliably
  reach the device from Home Assistant or push an OTA update.

### Controls

- Physical buttons: GPIO3 ("Next"), GPIO5 ("Prev") — both just call
  `enqueue_display_card` directly, no busy-flag check anywhere. GPIO4 is
  `deep_sleep`'s `wakeup_pin` and can't *also* be a bound `binary_sensor`;
  a press on it just looks like any other timer wake to `task_boot` — there's
  no special button-wake handling any more (this fell out naturally once
  `idle_scheduler` stopped needing to know *why* it's running).
- Home Assistant: "Next Card" and "Reload Manifest" buttons, "Diagnostics
  Card"/"Deep Sleep Enabled"/"OTA Mode" switches, and the interval/period/
  "Sleep Delay" entities — all exposed automatically via ESPHome's native
  API.
- `firmware_version` (substitutions block, top of the firmware YAML) —
  bump by hand on any meaningful change. Shown on the diagnostics screen
  ("Firmware: vX.Y.Z") and as the ESPHome project version (visible in HA's
  device info page), both from the same substitution.
- Nothing checks a bare "busy" boolean before acting any more. Every
  trigger enqueues through `enqueue_update_manifest`/
  `enqueue_download_cards`/`enqueue_display_card`; `task_dispatcher` is
  the only thing that decides whether to run something now (nothing else
  active) or leave it pending (something's already running — the request
  isn't dropped, it's serviced once the current task finishes).
  Repeated same-type requests coalesce (rapid "Next" presses sum into
  `pending_display_steps` and render once caught up) rather than firing a
  rapid-fire catch-up burst once the device frees up, which is what an
  earlier, more naive version of the busy-guard did — this only actually
  holds because every `enqueue_*` script is `mode: queued`; see "Every
  hand-back script needs `mode: queued`" below.

### Deep sleep invariant

`deep_sleep.prevent` is called unconditionally as the first action of
every boot, regardless of the "Deep Sleep Enabled" switch. Nothing else
in the file ever calls `deep_sleep.allow` except `task_sleep`, directly
before its own `deep_sleep.enter` — so the `run_duration` (90s)
safety-net timer is structurally incapable of firing mid-task; it's only
ever armed for the brief window inside the one task built to enter sleep.
"Deep Sleep Enabled" and "OTA Mode" are both plain config booleans now,
read only by `idle_scheduler` — neither switch touches
`deep_sleep.allow`/`prevent` directly any more (that direct-call pattern
is what let the safety net re-arm at an uncoordinated moment in the first
place).

The `gooddisplay_gdey075t7.cpp` `display()` method logs
`display() #N/30: FULL/partial refresh` — useful for confirming whether a
slow, flashy redraw was the periodic de-ghosting full-refresh (every 30th
update by default in v0.2) rather than a bug.

### Every hand-back script needs `mode: queued`

`script.execute` keeps the *calling* script marked "running" for as long
as the callee — transitively, including whatever the callee itself calls,
and any delay inside that chain — is still active, not just until the
callee's first yield. So any script that can be reached both as a nested
hand-back (e.g. `idle_scheduler` calling back into itself via
`idle_wait`) *and* as a fresh top-level call arriving while that nested
chain is still going (a button press) is exposed to ESPHome's default
`mode: single` behavior: the second call is silently **dropped**, not
queued, logged only as `Script 'X' is already running!`. For
`enqueue_display_card` specifically this isn't just a log line — the
dropped call's `pending_display_steps += steps` lambda never runs, so a
real button press vanishes with no effect at all.

`mode: queued` is the fix (ESPHome's own run-after-current-finishes
semantics), and every script sitting on this hand-back graph needs it:
`task_dispatcher`, `enqueue_update_manifest`, `enqueue_download_cards`,
`enqueue_display_card`, `idle_scheduler`, `idle_wait`, `task_sleep`. A
delay-based yield does **not** fix this — tried first, it just relocates
the same rejection onto whatever script the delay lives in, confirmed the
hard way on real hardware.

One remaining sharp edge worth knowing: if a bug causes an abnormal
backlog of queued instances (this happened once, caused by the
`elapsed_since_rotate` bug above generating dozens of queued dispatcher
passes in a couple of minutes), draining that backlog can briefly let
more than one instance reach "nothing pending, sleep" nearly
simultaneously right as the last queued render is still in flight —
observed once as a `Timeout while displaying image!` error moments after
`Beginning sleep` was logged, i.e. a render racing actual hardware
power-down. `task_sleep`'s `mode: queued` prevents *multiple*
`deep_sleep.enter` calls from that scenario, but the real fix is not
generating the backlog in the first place — this is a symptom to watch
for, not a separate bug to chase if the queue stays small under normal
operation.

### Why v0.2 was replaced

- **It builds a scheduler on top of ESPHome's scheduler.** The dispatcher,
  `pending_*` flags, `idle_scheduler` and `idle_wait` recreate an event
  loop out of scripts that call each other in a cycle. In ESPHome,
  `script.execute` runs the callee *inline, on the caller's stack*, until
  the callee reaches its first `delay`/`wait_until` (see
  `esphome/components/script/script.h`). So dispatcher -> task ->
  dispatcher -> idle_scheduler -> idle_wait -> idle_scheduler is one
  nested call chain, which is the real source of the `already running`
  warnings. The "Every hand-back script needs `mode: queued`" section
  above misreads this as the caller staying "running" for the callee's
  whole lifetime; each `mode: queued` treated a symptom of the re-entrancy.
  Queued scripts have no default `max_runs`, so the queues can grow
  without bound.
- **The loop's only heartbeat is a queued script sitting in a `delay`.**
  A button press that reaches `idle_scheduler` while an older `idle_wait`
  is still counting down queues a second wait behind it, with a duration
  computed at the wrong time. Scratch globals (`idle_next_action`,
  `idle_wait_s`) are shared across the nested invocations.
- **The stay-awake controls live on the device.** "OTA Mode" is RAM-only
  (`RESTORE_DEFAULT_OFF`) and can only be flipped during a wake window,
  hence the "Sleep Delay" catch window.
- **Every wake re-downloads everything.** PSRAM loses power in deep
  sleep, so the manifest and every card are fetched on every wake.
- **The "RTC memory" comments are wrong.** On ESP32, `globals` with
  `restore_value: true` go to NVS (flash), not RTC memory (see
  `components/esp32/preferences.cpp`: the default `make_preference()` is
  `in_flash=true`). They survive deep sleep, but writing them every wake
  wears flash.

### From proposal to build

The v0.3 design was written as a proposal on 2026-09-28 and built in steps
over that day and the next; the sections above describe what was built. A
few proposed pieces changed on the way: the green button's "sync now" and a
separate maintenance mode became service mode, the diagnostics card stopped
being a carousel item, the planned single retained JSON settings message (fed
by HA helpers and an automation) became the device's own MQTT controls, and
the `stay_awake` setting was dropped in favor of service mode and the deep
sleep switch.
