# waveshare_epaper (vendored)

The reTerminal E1001's panel is a GoodDisplay GDEY075T7, not the Waveshare
panel Seeed's ESPHome docs assume (see NOTES.md, "Hardware quirks"). This
driver comes from [parkghost/esphome-epaper](https://github.com/parkghost/esphome-epaper),
commit [`c6ac50c`](https://github.com/parkghost/esphome-epaper/tree/c6ac50cfb0b78a70331c4b70221a20af400c59aa/components/waveshare_epaper)
(2026-07-04), under the Apache License 2.0 (`LICENSE` in this directory).

It's vendored rather than pulled with `external_components: source:
github://...` because the firmware depends on local changes a live pull
would overwrite.

## Changes from upstream

- `gooddisplay_gdey075t7.h`, `gooddisplay_gdey075t7.cpp`:
  - Color polarity: frames go to the panel as ESPHome renders them.
    Upstream inverts each byte for full refreshes and sets an inverting
    DDX bit for partial ones, which came out as a negative on this unit.
  - Deep-sleep support. The partial-refresh base frame and refresh counter
    can be restored after a wake, the framebuffer is exposed for the flash
    card cache, setup skips the panel reset (about 234 ms per wake), busy
    waits can light-sleep the ESP32, per-phase timing is recorded, and the
    idle timeout is longer for cold rooms.

  NOTES.md, "Hardware quirks", has the background, including why the
  firmware sets `busy_pin: inverted: false`.
- `display.py`: registers only the `gdey075t7` model.
- Removed: the drivers for the other panels upstream supports (e0213a09,
  gdeh029a1, gdem029t94, gdew029t5d, gdey029z95, gdew042m01, depg0420,
  gdew042z15, p750057-mf1-a).

`waveshare_epaper.h`, `waveshare_epaper.cpp` and `__init__.py` are
unchanged from upstream.
