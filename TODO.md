# TODO

## Publishing on GitHub

Goal: a minimal repo that friends with a reTerminal E1001 and their own Home
Assistant can use to get the **weekly and daily energy cards** running with
their own sensors. Built from a copy of the original project, which stays
as it is. License: GPL-3.0. The vendored display driver is Apache-2.0
(parkghost/esphome-epaper), which a GPL-3.0 project can include.

### Phase 1: Scrub and generalize

- [x] Firmware split: shared base `firmware/wildstak.yaml`, per-user
      `firmware/device.yaml` (gitignored) that includes it as a package;
      `device.example.yaml` ships. Validated with `esphome config` (2026.9.0),
      including the optional static IP and TLS overrides.
- [x] Substitutions for manifest URL/host, MQTT broker/port/username,
      timezone, NTP server. DHCP and plain MQTT by default.
- [x] Status screen shows the device's real IP.
- [x] `firmware/secrets.example.yaml`.
- [x] Server timezone from `config.toml` (`[global] timezone`), no more JST.
- [x] `dashboards/config.example.toml`: the two energy cards, example
      entities; `key_file` now points at the repo-root `.env`.
- [x] Makefile: `BASE_URL` from a gitignored `local.mk`, with a clear error
      when it's missing.
- [x] `tools/ha_sensors.py`: lists kWh/W sensors, pairs them by HA device,
      prints `[[entities]]` tables. Pairing logic tested offline; the live HA
      calls (states + template API) still need a run against a real instance.
- [x] `.gitignore` for settings, secrets, and build output.
- [x] `certs/README.md` generalized.

### Phase 2: Trim

- [x] Display driver: traced to parkghost/esphome-epaper @ c6ac50c
      (2026-07-04); 22 of 24 files were unmodified upstream, local changes
      only in `gooddisplay_gdey075t7.{h,cpp}`. Removed the 9 other panel
      drivers (18 files) and their `display.py` entries. Provenance and
      changes in the component's README.md. Full firmware compile passes
      (ESPHome 2026.9.0; it caught a Phase 1 bug: `IPAddress::str()` doesn't
      exist on IPv4 builds, now `str_to()`; firmware_version 0.3.3).
- [x] `dashboardlib.py`: everything is used by the energy cards except
      `save_thresholded_image`, kept because DASHBOARD_SPEC.md makes it the
      required last step for PIL-drawn cards (what friends will write).

### Phase 3: Licensing

- [x] `LICENSE`: GPL-3.0, canonical text from GitHub's license API.
- [x] Display driver: upstream's Apache-2.0 `LICENSE` copied next to it, and
      the three modified files carry a "Modified for WildStak" header (Apache
      2.0 section 4(b)).
- [ ] Note for the README: the driver's base classes look derived from
      ESPHome's own `waveshare_epaper` (CODEOWNERS @clydebarrow), which is
      GPLv3; fine under a GPL-3.0 repo either way.
- [x] Fonts: both may be bundled; the copies in the repo are byte-identical
      to the original downloads. EnkaDotGothic24 is SIL OFL 1.1 (Enthiesia;
      its `OFL.txt` copied unchanged). x12y16pxMaruMonica is hicc's custom
      license (redistribution, bundling, commercial use allowed; credit
      optional; no selling the font), quoted in `dashboards/fonts/README.md`
      with the date checked.
- [x] Masthead: only `assets/masthead.png` and the trimmed ResEdit sprite in
      `tools/masthead.py` are in the repo; Chicago and Geneva are not.
- [ ] README (Phase 5): a short licensing section pointing at `LICENSE`,
      the driver's README, and `dashboards/fonts/README.md`.

### Phase 4: Packaging

- [x] `requirements.txt`: Pillow 12.3.0, matplotlib 3.10.6, numpy 2.4.3,
      polars 1.25.2, requests 2.34.2 (Python 3.12 tested, 3.11 minimum).
      A fresh venv from it installs cleanly (`pip check` passes).
- [x] Makefile: `make` (default `cards`) renders PNGs + manifest under
      docs/ for any static web server; `make site` adds the index page and
      the Zensical build. `BASE_URL` is checked before rendering, and
      `PYTHON` defaults to `.venv/bin/python` when it exists. Tested with
      fake cards: missing BASE_URL fails fast, `make dashboards` doesn't need
      it.
- [x] End to end against a live HA from the fresh venv: both cards render;
      weekly is pixel-identical to the original project's output, and
      daily too when rendered simultaneously (otherwise a few pixels of
      new live data). So the timezone refactor changed nothing visible.

### Phase 5: Docs

- [x] README: masthead, what it is, how it works, what you need, setup
      (cards, Home Assistant, firmware), using it, repository map, license,
      credits.
- [x] NOTES.md restructured: current design first (as-built sections from
      steps 4 and 5 lead), measurements, manifest format, open questions,
      ideas, hardware quirks, then "History: v0.2". Proposal-only sections
      that the build superseded are summarized in History; site specifics
      (hostnames, IPs, server repo, HA dashboard files) and references to
      files not in this repo are gone.
- [x] DASHBOARD_SPEC.md: timezone instead of JST; going_out noted as not
      included; card scripts named `*_dashboard.py` (the spec said
      `my_card.py`, which the Makefile would never pick up).
- [x] `tools/ha_sensors.py` run against a live HA: fixed two bugs it showed
      (exported-energy sensors listed as devices; a device holding two plugs
      left unpaired). It now reproduces the hand-written config.
- [x] Driver README: added the color polarity change (also a local patch).
- [ ] Decide: replace the em dashes in NOTES.md / DASHBOARD_SPEC.md (70,
      from the original text)?
- [ ] Decide: sample card PNGs in the README (would show real usage data)?

### Phase 6: Publish

- [x] Final scan for secrets, IPs, hostnames, and personal names: clean
      (only placeholder IPs and the intended `ryneches` project ID / URL).
- [x] First commit; public repo at github.com/ryneches/wildstak, pushed.

## Rendering pathway (low priority / research only)

`weekly_energy_dashboard.py` and `daily_energy_dashboard.py` render via
matplotlib and hard-threshold to 1-bit at the end (see DASHBOARD_SPEC.md).
They already look good on the actual hardware: **not worth migrating off
matplotlib for its own sake.** Worth revisiting only if a library turns up
that draws sparkline-like charts *natively* in 1-bit (no antialiasing to
threshold away) without giving up what matplotlib is doing for free today
(axis scaling, interpolation, fill-between).

- [ ] (optional) Look for a native 1-bit / no-antialiasing charting
      library that could replace matplotlib for the sparkline/plot cards.
      No known candidate yet as of 2026-09-21; not blocking anything.
