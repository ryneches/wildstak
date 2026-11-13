# Dashboard authoring spec

This is the contract and style guide for adding a new dashboard card to the
WildStak display (a reTerminal E1001). It's written for whoever (human or LLM)
writes the next one — it should be enough to produce a new card without
re-deriving the conventions below by trial and error.

For how the *device* consumes these PNGs (manifest, prefetch, rotation),
see `NOTES.md`. This document is scoped to *producing* a dashboard PNG.

## System shape

```
config.toml ──┬─→ dashboardlib.py ─→ each dashboards/*_dashboard.py ─→ one PNG
              └─→ (per-card [cards.X] table)
```

- `dashboards/dashboardlib.py` — shared library. Owns the config/CLI
  contract, HA fetch helpers, font setup, the monochrome render/threshold
  postprocessing, and the shared panel geometry. Not a card itself: its
  name doesn't match `*_dashboard.py`, so the Makefile never runs it.
- `dashboards/config.toml` — global settings + one `[cards.<name>]` table
  per dashboard. See "Config reference" below.
- `dashboards/<card_name>_dashboard.py` — one independently-runnable script
  per card. The `_dashboard.py` suffix is what the Makefile looks for.
  This is where all card-specific layout and rendering logic lives.

Adding a new dashboard means: write `dashboards/my_card_dashboard.py`, add a
`[cards.my_card]` table to `config.toml`, done. No changes to
`dashboardlib.py` or the Makefile.

## Rendering pathway: PIL by default, matplotlib for chart ink only

**Default: draw the whole card with PIL's `ImageDraw`, in plain integer
device pixels.** (The reference implementation was the original project's
`going_out_dashboard.py`, a train timetable and weather card; it isn't in
this repo, so both cards here are matplotlib ones.)
Create the canvas with `Image.new("L", (W, H), 255)` and draw directly with
`draw.line()` / `draw.rectangle()` / `draw.text()` — no axes-fraction
coordinate space, no figure/DPI arithmetic, no intermediate scene graph.
Finish with `dlib.save_thresholded_image(img, output)` (same hard
threshold-to-1-bit contract as the matplotlib path, just applied to a PIL
`Image` directly — in practice this font already renders pure black/white
with no antialiasing, verified, so the threshold is a safety net rather
than where the real work happens).

This is the default because it matches what the target actually is: a fixed
integer pixel grid, the same way classic Mac OS's QuickDraw treated 1-bit
displays. In QuickDraw, "draw at column 10" painted column 10 — the pixel
was the coordinate, full stop. In PIL/ImageDraw it's the same: draw two
lines at columns 10 and 12 and you get exactly one white pixel at column
11, verified directly against rendered output, no calibration needed.
matplotlib's Agg backend instead builds a scene graph in continuous
floating-point space and only meets the pixel grid at the very last
rasterization step — a "nominal 1px gap" and an "actual rendered 1px gap"
are not reliably the same thing there. That mismatch cost this project
three separate rounds of "the gap I asked for isn't showing up" bugs
before the first PIL card moved off matplotlib entirely; don't reintroduce
it in a new pure-layout card.

One caveat that *does* carry over: even PIL's `textbbox` isn't perfectly
ink-tight — it can overshoot true glyph ink by a small, asymmetric amount
(a pixel or two, unevenly distributed per side), not zero. If a design
calls for an ink-tight box, measure the actual rendered, thresholded PNG
with numpy before trusting bbox values as exact, and calibrate any margin
constants from that measurement. Don't assume a bbox is exact just because it's much closer
than matplotlib's was.

**Chart-bearing cards (a real data plot): matplotlib renders only the
plot's own ink, at minimal chrome, into a sub-rectangle; PIL does
everything else.** Build the matplotlib figure with every non-essential
element turned off — no spines/ticks/labels/legend beyond what the data
itself needs (`setup_matplotlib()`'s antialiasing-off rcParams still
apply) — render it to exactly the pixel size of its target rectangle, and
paste that into the PIL canvas at the right position. All labels, titles,
dividers, and any other layout chrome around the chart are then drawn with
`ImageDraw`, not matplotlib. matplotlib is doing real, load-bearing work
here (axis scaling, interpolation, fill-between) that would be painful to
reimplement by hand — this pathway isn't about avoiding matplotlib, only
about not asking it to also be a layout/typography engine, which is the
part it's weak at.

No card has needed this pathway yet (`weekly_energy_dashboard.py` and
`daily_energy_dashboard.py` predate this policy and are all-matplotlib,
including their chrome — leave them as they are unless you're touching
them for another reason). Don't build the composite-into-PIL helper
speculatively; write it against the first real chart card that needs it,
the same way `save_thresholded_image` was added alongside the first
PIL-based card rather than in advance.

## The CLI/config contract

Every dashboard script's `main` entrypoint is the same shape:

```python
CARD_NAME = "my_card"                     # matches [cards.my_card] in config.toml

_parser = dlib.build_arg_parser(
    "Render the <whatever> dashboard PNG.",
    default_output=Path(__file__).parent / "my_card.png",
)
_args = _parser.parse_args()
CFG = dlib.load_dashboard_config(CARD_NAME, _args)
```

`build_arg_parser` gives every script the same four flags:

| Flag | Overrides | Why it's CLI, not just config |
|---|---|---|
| `--config` | which config.toml to read | lets you point at an alternate config for testing |
| `--ha-url` | `[global].ha_url` | deployment-specific (dev/prod HA instance) |
| `--key-file` | `[global].key_file` | deployment-specific (secret path varies per machine) |
| `-o/--output` | output path | the Makefile always overrides this to `docs/media/<name>.png` |

That's the whole CLI surface, deliberately. **Fonts, sizes, entities, and
row_fill are config-only, not CLI flags.** The principle: *deployment*
parameters (which HA instance, where the secret lives) are CLI-overridable;
*design* parameters (what it looks like) live only in `config.toml`. If
you're tempted to add a new CLI flag, ask whether it's actually a
deployment concern — if not, it belongs in config.toml instead, even if
that means editing the file instead of passing a flag during testing.

`CFG` (a `dlib.DashboardConfig`) is your single source of truth for
everything global plus your card's `title_left`/`title_right`/`row_fill`.
Card-specific data beyond entities and titles (see below) isn't part of
this dataclass — if your card needs its own tunable, add it as a plain
module-level constant in your script, or extend `[cards.my_card]` and read
it directly from the parsed TOML if it needs to flow through `dashboardlib`
too (see "Extending the config" below).

## Config reference (`config.toml`)

```toml
[global]
ha_url = "..."
key_file = "../.env"
timezone = "Europe/Berlin"   # IANA name: day boundaries, time axes
canvas_width = 800
canvas_height = 480
dpi = 72
row_fill = 0.53          # default; a card can override its own row_fill

[global.label_font]
path = "fonts/x12y16pxMaruMonica.ttf"   # relative to config.toml's own dir
size_dev = 16
size_small = 12

[global.title_font]
path = "/absolute/or/relative/path.ttf"
size = 24

[[entities]]              # shared across all cards
energy = "sensor.foo_energy"
power  = "sensor.foo_power"   # optional; omit if a card never needs power history
label  = "Foo"

[cards.my_card]
title_left  = "..."
title_right = "..."
# row_fill = 0.6           # optional per-card override
```

Relative paths in `[global]` (font paths, `key_file`) resolve against
`config.toml`'s own directory, not the current working directory — so the
Makefile can invoke scripts from anywhere and paths still work.

**Entities are shared, not per-card**, because every card so far draws from
the same household device list. Adding a device is: add one `[[entities]]`
block, and every card that iterates `CFG.entities` picks it up automatically
with rows/bars auto-sized to fit (see "Row geometry" below) — no per-card
edits needed. If a future card genuinely needs a different subset of
entities, that's a real extension point; don't build it preemptively.

### Extending the config

If a new card needs a genuinely new *design* parameter (not deployment,
not something covered by row_fill), add it to that card's
`[cards.my_card]` table and read it directly:

```python
import tomllib
with open(_args.config, "rb") as f:
    _raw = tomllib.load(f)
MY_EXTRA = _raw["cards"]["my_card"].get("my_extra", <default>)
```

Don't add it to the `DashboardConfig` dataclass in `dashboardlib.py` unless
it's something every card should plausibly have (that dataclass is the
*global* contract, not a per-card grab-bag).

## Design principles

These aren't arbitrary — each one exists because we tried the alternative
and it looked worse on the actual hardware. See `NOTES.md` for the
hardware-side reasoning (panel identity, refresh behavior); this section is
about the rendering choices on the software side.

**Pixel-perfect monochrome, no dithering, no grayscale.** The display is
truly 1-bit. Every dashboard renders normally in matplotlib, then gets
hard-thresholded to pure black/white via `dlib.save_thresholded()` — call
that instead of `fig.savefig()` directly, or your PNG will still have
antialiased gray edges that threshold unpredictably. `dlib.setup_matplotlib()`
must be called once at import time (every existing script does this at
module level, not inside `main()`) so antialiasing is off *before* anything
gets drawn — turning it off after some artists already exist doesn't
retroactively fix them.

**Flat fills only.** No dithering to simulate gray. Hatching is fine as a
genuine visual pattern (not currently used, but wouldn't be out of place),
never as a gray-simulation trick.

**Trust `lw=1` to mean exactly 1 physical pixel.** DPI is fixed at 72, so
1pt = 1px with no conversion. Prefer plain `ax.plot([x0,x1],[y0,y1], lw=1,
color="black")` for rule lines over `axhline`/`axvline` when you want a
crisp, precisely-positioned 1px line with butt caps — the convenience
functions sometimes span the whole axes when you want a partial-width rule.

**Two-tier font system, both registered via `dlib.setup_fonts(CFG)`:**
- *Label font* (`_FONT` in existing scripts): device labels, tick labels,
  values. Currently a genuine pixel font with a native 16px grid — if you
  introduce a new label font, know that pixel fonts often only look crisp
  at their native size or clean integer multiples of it; test at a few
  sizes before committing (we found the current label font mixes 1px/2px
  strokes unevenly at any non-native size, which is why titles use a
  separate font entirely rather than just scaling the label font up).
- *Title font* (`_TITLE_FONT`): section titles only, larger and bolder-looking
  by design (chosen specifically for staying crisp at a bigger size, not
  reused from the label font). Use `dlib.title_y(LAYOUT, body_top_axes_frac)`
  to position it — this centers the title in the *actual visible gap*
  between the screen's top edge and the chart body, accounting for the
  extra margin reserved for the panel's physical inset shadow. Don't
  hand-roll `1.0 - title_h/2`; it looks off-center once you account for
  that margin.

**Row geometry.** If your card lays out one row per entity (like both
existing cards do), row height is always `_BODY_H / n_entities` — this
means adding entities to config.toml automatically shrinks every row to
fit, no code changes required. `CFG.row_fill` (default 0.53, overridable
per-card) is the one layout knob meant to be hand-tuned: it's the fraction
of each row's height actually used by the bar/plot, with the remainder as
gap. If a card's rows look too cramped or too sparse after an entity count
change, adjust `row_fill` in that card's config table — don't add new
per-row spacing constants.

**Shared panel geometry is not a per-card knob.** `dlib.compute_layout()`
returns the label column width, grid/sparkline/right-panel positions, and
title/day-label strip heights — identical for every card on purpose, so a
device's row lands in the same place and the vertical divider lines up
regardless of which card is currently on screen. If a new card's layout is
different enough that it can't use `compute_layout()`'s panels as-is (e.g.
a single full-bleed chart with no label column), that's fine — just don't
partially reuse the shared constants in a way that breaks alignment for
the *other* cards. A layout that ignores the shared geometry entirely is
better than one that quietly drifts from it.

**HA fetching.** Use `dlib.fetch_states`, `dlib.fetch_first_values`,
`dlib.fetch_power_history` — all take `CFG` as the first argument. For
anything wall-clock (day boundaries, axis labels, "now"), use `CFG.tz`,
the `[global] timezone` from config.toml; HA's timestamps are UTC. If you need more than ~1000 history records in one
query, chunk it into per-midnight queries like the existing scripts do —
Home Assistant silently truncates long single-query results rather than
erroring, which is easy to miss.

## Adding a new dashboard: checklist

1. Add a `[cards.my_card]` table to `config.toml` with at least
   `title_left`/`title_right` (or whatever your card's own titles are).
2. Write `dashboards/my_card_dashboard.py`. Which reference to copy the `# Config`
   boilerplate from depends on which rendering pathway applies (see
   "Rendering pathway" above):
   - **Pure layout/typography, no data plot** (the default): there's no
     PIL card in this repo to copy yet. Take the `# Config` block from
     `weekly_energy_dashboard.py` (CARD_NAME, `build_arg_parser`,
     `load_dashboard_config`, `compute_layout`), load fonts directly with
     `ImageFont.truetype()`, and draw with `ImageDraw` in plain integer
     pixels.
   - **Has a real chart**: copy from `weekly_energy_dashboard.py`
     (CARD_NAME, `build_arg_parser`, `load_dashboard_config`,
     `setup_fonts`, `setup_matplotlib`, `compute_layout`) for the
     matplotlib-rendered plot itself, but composite it into a PIL canvas
     per the "Rendering pathway" section rather than drawing the
     surrounding labels/titles/dividers in matplotlib too.
3. Write your `render()` function. Use `CFG.entities` for device data,
   `dlib.title_y()` for title placement, `dlib.fit_text_to_area()` if you
   have a "make this number big" moment (matplotlib path), and finish with
   `dlib.save_thresholded()` (matplotlib `Figure`) or
   `dlib.save_thresholded_image()` (PIL `Image`) at the end.
4. Give the module a docstring — the Makefile's `dashboards_page` target
   scrapes it verbatim into `docs/dashboards.md`.
5. Run it directly (`python3 dashboards/my_card_dashboard.py --key-file .env`),
   inspect the PNG, iterate. Once it's right, `make` picks it up
   automatically — no Makefile changes.
