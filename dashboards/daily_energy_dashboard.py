#!/usr/bin/env python3
"""
daily_energy_dashboard.py - 800x480 monochrome 24-hour power dashboard for reTerminal.

Left panel (584px):
  label column (80px) + 504px data area
  8 stacked line plots, one per device, showing last 24h of power in W.
  Vertical extent matches the grid body in weekly_energy_dashboard.py.

Right panel (~202px):
  Cumulative kWh per device over the last 24h (rolling window, not calendar day).

Bottom strip (504px):
  Total household wattage over last 24h.
  Nighttime: solid black fill.  Daytime: white fill.
  Vertical dashed lines at 00:00, 06:00, 12:00, 18:00 local time (shown above the plot).

Bottom-right:
  Total kWh for the last 24h.

Entities, fonts, and titles come from config.toml, not this file - see
DASHBOARD_SPEC.md for the full config/CLI contract and the style
conventions (pixel-perfect rendering, no dithering, font choices) this
script follows.

Usage: python daily_energy_dashboard.py [--config CONFIG.toml]
       [--ha-url HA_URL] [--key-file KEY_FILE] [-o OUTPUT.png]
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.axes as maxes
import matplotlib.dates as mdates
import matplotlib.lines as mlines
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import polars as pl
import requests

import dashboardlib as dlib

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CARD_NAME = "daily_energy"

_parser = dlib.build_arg_parser(
    "Render the daily energy dashboard PNG.",
    default_output=Path(__file__).parent / "daily_energy_dashboard.png",
)
_args = _parser.parse_args()
CFG = dlib.load_dashboard_config(CARD_NAME, _args)

OUTPUT = CFG.output

_FONT, _TITLE_FONT = dlib.setup_fonts(CFG)

FS_MAIN = CFG.title_font_size   # section titles
FS_DEV = CFG.label_font_size_dev
FS_SMALL = CFG.label_font_size_small

dlib.setup_matplotlib(_FONT, FS_SMALL)

TZ = CFG.tz
W, H, DPI = CFG.canvas_width, CFG.canvas_height, CFG.dpi
# At DPI=72: 1pt = 1px exactly. lw=1 is one physical pixel; no compensation needed.

DEVICES: dict[str, str] = {e.energy: e.label for e in CFG.entities}
POWER_MAP: dict[str, str | None] = {e.energy: e.power for e in CFG.entities}

POWER_SCALE = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000]

# ---------------------------------------------------------------------------
# Layout constants: identical geometry to weekly_energy_dashboard.py
# Shared geometry lives in dashboardlib.compute_layout(); see DASHBOARD_SPEC.md.
# ---------------------------------------------------------------------------

LAYOUT = dlib.compute_layout(W, H)

L_GRID, W_GRID, LABEL_F  = LAYOUT.l_grid, LAYOUT.w_grid, LAYOUT.label_f
L_SPARK, W_SPARK         = LAYOUT.l_spark, LAYOUT.w_spark
B_SPARK, H_SPARK         = LAYOUT.b_spark, LAYOUT.h_spark
B_GRID, H_GRID           = LAYOUT.b_grid, LAYOUT.h_grid
_DIV_X, L_RIGHT, W_RIGHT = LAYOUT.div_x, LAYOUT.l_right, LAYOUT.w_right
_TITLE_H, _DAYLABEL_H, _BODY_H = LAYOUT.title_h, LAYOUT.daylabel_h, LAYOUT.body_h

ROW_FILL = CFG.row_fill

# Body area in figure coordinates for the 8 device sub-axes.
# Same top and bottom as the grid body in weekly_energy_dashboard.py.
_BODY_TOP_FIG = B_GRID + (1.0 - _TITLE_H) * H_GRID
_BODY_BOT_FIG = B_GRID + _DAYLABEL_H * H_GRID
_BODY_H_FIG = _BODY_TOP_FIG - _BODY_BOT_FIG

# ---------------------------------------------------------------------------
# HA sun helper (not part of dashboardlib - specific to this card's day/night fill)
# ---------------------------------------------------------------------------


def fetch_sun_info(
    now: datetime,
) -> tuple[list[tuple[str, datetime]], datetime | None, datetime | None]:
    """
    Return (transitions, today_sunrise, today_sunset) from sun.sun history over
    the last 24h. transitions is a list of (state, local datetime) pairs in
    chronological order, including the initial state before the window.
    """
    win_start = now - timedelta(hours=24)
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    url = (
        f"{CFG.ha_url}/api/history/period/{win_start.isoformat()}"
        f"?filter_entity_id=sun.sun&minimal_response=true"
    )
    r = requests.get(url, headers=dlib.headers(CFG), timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data or not data[0]:
        return [], None, None

    transitions: list[tuple[str, datetime]] = []
    sunrise: datetime | None = None
    sunset: datetime | None = None
    prev_state: str | None = None

    for rec in data[0]:
        state = rec.get("state", "")
        ts_str = rec.get("last_changed") or rec.get("last_updated", "")
        if not ts_str or not state:
            continue
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(TZ)

        if prev_state is None:
            transitions.append((state, ts))
        elif state != prev_state:
            transitions.append((state, ts))
            if state == "above_horizon" and ts >= today_midnight and sunrise is None:
                sunrise = ts
            elif state == "below_horizon" and ts >= today_midnight and sunset is None:
                sunset = ts
        prev_state = state

    return transitions, sunrise, sunset


def _is_day(t: datetime, transitions: list[tuple[str, datetime]]) -> bool:
    """Return True if t is daytime, determined by walking sun.sun transition history."""
    current = "below_horizon"
    for state, ts in transitions:
        if ts > t:
            break
        current = state
    return current == "above_horizon"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_data(
    now: datetime,
) -> tuple[
    pl.DataFrame,
    dict[str, float],
    float,
    list[tuple[str, datetime]],
]:
    all_eids = list(DEVICES.keys())
    power_eids = [p for p in POWER_MAP.values() if p]
    states = dlib.fetch_states(CFG)

    # Rolling 24h anchor: energy delta = current state - state at (now - 24h).
    win_start = now - timedelta(hours=24)
    start_vals = dlib.fetch_first_values(CFG, all_eids, win_start)
    print(f"  24h-ago anchors: {len(start_vals)}/{len(all_eids)} sensors")

    rolling_kwh: dict[str, float] = {}
    for eid in all_eids:
        try:
            rolling_kwh[eid] = max(
                0.0,
                float(states[eid]) - start_vals.get(eid, float(states[eid])),
            )
        except (ValueError, KeyError):
            rolling_kwh[eid] = 0.0

    # Power history: 2 daily midnight queries covers any 24h rolling window.
    print("  Fetching 24h power history...")
    power_frames: list[pl.DataFrame] = []
    for d in range(2):
        midnight = (now - timedelta(days=d)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        df = dlib.fetch_power_history(CFG, power_eids, midnight)
        power_frames.append(df)
        print(f"    day -{d}: {len(df)} records")

    win_start_utc = win_start.astimezone(timezone.utc)
    power_history = (
        pl.concat(power_frames)
        .unique(subset=["ts", "entity_id"])
        .filter(pl.col("ts") >= win_start_utc)
        .sort("ts")
    )

    print("  Fetching sun times...")
    transitions, sunrise, sunset = fetch_sun_info(now)
    print(f"    sunrise={sunrise}, sunset={sunset}, transitions={len(transitions)}")

    return power_history, rolling_kwh, sum(rolling_kwh.values()), transitions


# ---------------------------------------------------------------------------
# Bin aggregation
# ---------------------------------------------------------------------------


def make_device_bins(
    power_history: pl.DataFrame,
    win_start: datetime,
    win_end: datetime,
) -> pl.DataFrame:
    """
    Return wide DataFrame with one row per display pixel (DATA_PX bins) and
    one column per device label, containing mean watts in that pixel interval.
    """
    if power_history.is_empty():
        return pl.DataFrame()

    win_start_us = int(win_start.astimezone(timezone.utc).timestamp() * 1_000_000)
    window_us = int((win_end - win_start).total_seconds() * 1_000_000)

    pmap_rev = {v: k for k, v in POWER_MAP.items()}
    label_map = {pid: DEVICES[eid] for pid, eid in pmap_rev.items() if eid in DEVICES}

    binned = (
        power_history.with_columns(
            (
                (pl.col("ts").dt.timestamp("us") - win_start_us).cast(pl.Float64)
                * LAYOUT.data_px
                / window_us
            )
            .cast(pl.Int64)
            .clip(0, LAYOUT.data_px - 1)
            .alias("px_bin")
        )
        .group_by(["px_bin", "entity_id"])
        .agg(pl.col("watts").mean())
        .with_columns(pl.col("entity_id").replace(label_map).alias("label"))
        .drop("entity_id")
    )

    wide = (
        binned.pivot(
            values="watts", index="px_bin", on="label", aggregate_function="mean"
        )
        .fill_null(0.0)
        .sort("px_bin")
    )
    return wide.with_columns(
        (
            pl.lit(win_start_us)
            + (pl.col("px_bin").cast(pl.Float64) * window_us / LAYOUT.data_px).cast(pl.Int64)
        )
        .cast(pl.Datetime("us", "UTC"))
        .alias("bin_time")
    )


def make_total_bins(
    power_history: pl.DataFrame,
    win_start: datetime,
    win_end: datetime,
) -> pl.DataFrame:
    """Sum all device watts per display pixel."""
    if power_history.is_empty():
        return pl.DataFrame(
            {
                "bin_time": pl.Series([], dtype=pl.Datetime("us", "UTC")),
                "total_w": pl.Series([], dtype=pl.Float64),
            }
        )

    win_start_us = int(win_start.astimezone(timezone.utc).timestamp() * 1_000_000)
    window_us = int((win_end - win_start).total_seconds() * 1_000_000)

    binned = (
        power_history.with_columns(
            (
                (pl.col("ts").dt.timestamp("us") - win_start_us).cast(pl.Float64)
                * LAYOUT.data_px
                / window_us
            )
            .cast(pl.Int64)
            .clip(0, LAYOUT.data_px - 1)
            .alias("px_bin")
        )
        .group_by(["px_bin", "entity_id"])
        .agg(pl.col("watts").mean())
        .group_by("px_bin")
        .agg(pl.col("watts").sum().alias("total_w"))
        .sort("px_bin")
    )
    return binned.with_columns(
        (
            pl.lit(win_start_us)
            + (pl.col("px_bin").cast(pl.Float64) * window_us / LAYOUT.data_px).cast(pl.Int64)
        )
        .cast(pl.Datetime("us", "UTC"))
        .alias("bin_time")
    )


def _to_local(b) -> datetime:
    if hasattr(b, "replace"):
        return b.replace(tzinfo=timezone.utc).astimezone(TZ)
    return b


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(
    power_history: pl.DataFrame,
    rolling_kwh: dict[str, float],
    total_kwh: float,
    transitions: list[tuple[str, datetime]],
    now: datetime,
    output: Path,
) -> None:
    eids = list(DEVICES.keys())
    labels = [DEVICES[e] for e in eids]
    n = len(eids)

    win_start = now - timedelta(hours=24)
    win_end = now

    device_bins = make_device_bins(power_history, win_start, win_end)
    total_bins = make_total_bins(power_history, win_start, win_end)

    fig = plt.figure(figsize=(W / DPI, H / DPI), dpi=DPI, facecolor="white")

    # ── Left structural axes (title + label column) ──────────────────────────
    ax_main = fig.add_axes((L_GRID, B_GRID, W_GRID, H_GRID))
    ax_main.set_xlim(0, 1)
    ax_main.set_ylim(0, 1)
    ax_main.axis("off")
    ax_main.text(
        0.5,
        dlib.title_y(LAYOUT, 1.0 - _TITLE_H),
        CFG.title_left,
        ha="center",
        va="center",
        fontsize=FS_MAIN,
        fontweight="normal",
        fontfamily=_TITLE_FONT,
        transform=ax_main.transAxes,
    )

    # Vertical rule separating label column from data area (body only, not daylabel strip)
    ax_main.plot(
        [LABEL_F, LABEL_F],
        [_DAYLABEL_H, 1.0 - _TITLE_H],
        color="black",
        lw=1,
        transform=ax_main.transAxes,
    )

    # ── Device line-plot sub-axes ─────────────────────────────────────────────
    row_h_fig = _BODY_H_FIG / n
    gap_fig = row_h_fig * (1.0 - ROW_FILL)
    # Right-align labels at the same figure-x as weekly_energy_dashboard.py's grid labels.
    label_rx = L_GRID + (LABEL_F - 0.012) * W_GRID

    for i, lbl in enumerate(labels):
        ax_bot = _BODY_TOP_FIG - (i + 1) * row_h_fig + gap_fig / 2
        ax_h = row_h_fig - gap_fig
        ax_dev = fig.add_axes((L_SPARK, ax_bot, W_SPARK, ax_h))
        fig.text(
            label_rx,
            ax_bot + ax_h / 2,
            lbl,
            ha="right",
            va="center",
            fontsize=FS_DEV,
            fontfamily=_FONT,
        )
        _render_device_line(
            ax_dev, device_bins, lbl, win_start, win_end, is_bottom=(i == n - 1)
        )

    # ── Total wattage sparkline ───────────────────────────────────────────────
    ax_spark = fig.add_axes((L_SPARK, B_SPARK, W_SPARK, H_SPARK))
    _render_total_sparkline(ax_spark, total_bins, win_start, win_end, now, transitions)

    # ── Right panel: rolling 24h kWh ─────────────────────────────────────────
    ax_right = fig.add_axes((L_RIGHT, B_GRID, W_RIGHT, H_GRID))
    _render_daily_summary(ax_right, rolling_kwh, eids)

    # ── Vertical divider ─────────────────────────────────────────────────────
    fig.add_artist(
        mlines.Line2D(
            [_DIV_X, _DIV_X],
            [B_SPARK, 0.995],
            transform=fig.transFigure,
            color="black",
            linewidth=1,
        )
    )

    # ── Total 24h kWh (lower-right) ───────────────────────────────────────────
    if total_kwh < 100:
        total_str = f"{total_kwh:.1f} kWh"
    else:
        total_str = f"{round(total_kwh):.0f} kWh"

    fs = dlib.fit_text_to_area(fig, total_str, W_RIGHT * W, B_GRID * H, _FONT)
    fig.text(
        L_RIGHT + W_RIGHT / 2,
        B_GRID / 2,
        total_str,
        ha="center",
        va="center",
        fontsize=fs,
        fontweight="bold",
        fontfamily=_FONT,
    )

    dlib.save_thresholded(fig, output, DPI)


def _render_device_line(
    ax: maxes.Axes,
    device_bins: pl.DataFrame,
    label: str,
    win_start: datetime,
    win_end: datetime,
    is_bottom: bool = False,
) -> None:
    ax.set_facecolor("white")
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines["left"].set_linewidth(1)
    ax.spines["bottom"].set_visible(is_bottom)
    if is_bottom:
        ax.spines["bottom"].set_linewidth(1)
    ax.set_xlim(win_start, win_end)
    ax.margins(x=0)
    ax.tick_params(which="both", length=0, labelbottom=False, labelleft=False)

    if device_bins.is_empty() or label not in device_bins.columns:
        return

    bins_py = [_to_local(b) for b in device_bins["bin_time"].to_list()]
    watts = device_bins[label].to_list()
    if not bins_py:
        return

    peak = max(watts) if watts else 0.0
    y_max = next((v for v in POWER_SCALE if v >= peak), POWER_SCALE[-1])
    ax.set_ylim(-0.05 * y_max, 1.05 * y_max)
    if is_bottom:
        ax.spines["left"].set_bounds(-0.049 * y_max, 1.05 * y_max)
    ax.plot(bins_py, watts, color="black", lw=1, drawstyle="steps-post")

    if peak > 0:
        drop = 3 / (ax.get_position().height * H)
        ax.text(
            7 / LAYOUT.data_px,
            1.0 - drop,
            f"{y_max:.0f}W",
            ha="left",
            va="top",
            fontsize=FS_SMALL,
            transform=ax.transAxes,
            zorder=5,
            bbox=dict(facecolor="white", edgecolor="black", linewidth=1, pad=2),
        )


def _fill_sparkline(
    ax: maxes.Axes,
    bins_py: list[datetime],
    watts: list[float],
    transitions: list[tuple[str, datetime]],
) -> None:
    """
    Fill under the sparkline with clean vertical cuts at sun transitions.

    Inserts a synthetic interpolated point at each day/night boundary so that
    fill_between never has to extrapolate across a mask edge, eliminating the
    diagonal glitch that appears with the where= / interpolate= approach.
    """
    if not bins_py:
        return

    # Collect transition timestamps that fall strictly inside the plot range.
    # transitions[0] is the initial state record (may predate the window);
    # transitions[1:] are actual state changes.
    change_times = sorted(
        ts for _, ts in transitions[1:] if bins_py[0] < ts < bins_py[-1]
    )

    # Build augmented time/watts arrays with a synthetic point at each transition.
    all_t: list[datetime] = list(bins_py)
    all_w: list[float] = list(watts)

    for t_sun in change_times:
        idx = next((i for i, t in enumerate(all_t) if t > t_sun), len(all_t))
        if 0 < idx < len(all_t):
            t0, t1 = all_t[idx - 1], all_t[idx]
            secs = (t1 - t0).total_seconds()
            frac = (t_sun - t0).total_seconds() / secs if secs > 0 else 0.0
            all_t.insert(idx, t_sun)
            all_w.insert(idx, all_w[idx - 1] + frac * (all_w[idx] - all_w[idx - 1]))

    # Fill each contiguous night / day segment.
    seg_start = 0
    prev_is_day = _is_day(all_t[0], transitions)

    for i in range(1, len(all_t)):
        is_day_i = _is_day(all_t[i], transitions)
        if is_day_i != prev_is_day:
            # Include the shared boundary point in both segments for clean edges.
            seg_t = all_t[seg_start : i + 1]
            seg_w = all_w[seg_start : i + 1]
            if prev_is_day:
                ax.fill_between(
                    seg_t, seg_w, 0, step="post", facecolor="white", linewidth=0
                )
            else:
                ax.fill_between(
                    seg_t,
                    seg_w,
                    0,
                    step="post",
                    facecolor="black",
                    edgecolor="none",
                    linewidth=0,
                )
            seg_start = i
            prev_is_day = is_day_i

    seg_t = all_t[seg_start:]
    seg_w = all_w[seg_start:]
    if prev_is_day:
        ax.fill_between(seg_t, seg_w, 0, step="post", facecolor="white", linewidth=0)
    else:
        ax.fill_between(
            seg_t,
            seg_w,
            0,
            step="post",
            facecolor="black",
            edgecolor="none",
            linewidth=0,
        )


def _render_total_sparkline(
    ax: maxes.Axes,
    total_bins: pl.DataFrame,
    win_start: datetime,
    win_end: datetime,
    now: datetime,
    transitions: list[tuple[str, datetime]],
) -> None:
    ax.set_facecolor("white")
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_linewidth(1)
    ax.set_xlim(win_start, win_end)
    ax.margins(x=0)

    if not total_bins.is_empty():
        bins_py = [_to_local(b) for b in total_bins["bin_time"].to_list()]
        watts = total_bins["total_w"].to_list()
        peak = max(watts) if watts else 1.0
        ax.set_ylim(0, peak * 1.15)

        _fill_sparkline(ax, bins_py, watts, transitions)
        ax.plot(bins_py, watts, color="black", lw=1, drawstyle="steps-post")

    # 6-hourly dashed marks (00:00, 06:00, 12:00, 18:00 local time)
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for day_offset in [-1, 0]:
        for h in [0, 6, 12, 18]:
            mark = today_midnight + timedelta(days=day_offset, hours=h)
            if win_start < mark < win_end:
                ax.axvline(mark, color="black", lw=1, linestyle=(0, (4, 3)))

    # X-axis hour labels float above the sparkline into the gap below device plots.
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 6, 12, 18], tz=TZ))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%-H:%M", tz=TZ))
    ax.tick_params(
        axis="x",
        top=True,
        labeltop=True,
        bottom=False,
        labelbottom=False,
        labelsize=FS_SMALL,
        pad=12,
        width=1,
        length=3,
    )

    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}"))
    ax.yaxis.set_major_locator(mticker.MaxNLocator(nbins=2, integer=True))
    ax.tick_params(axis="y", labelsize=FS_SMALL, pad=1, width=1, length=3)
    ax.set_ylabel("W", fontsize=FS_SMALL, labelpad=2)


def _render_daily_summary(
    ax: maxes.Axes,
    rolling_kwh: dict[str, float],
    eids: list[str],
) -> None:
    n = len(eids)
    ax.set_facecolor("white")
    ax.axis("off")
    ax.set_xlim(0.0, 1.0)
    ax.set_ylim(0.0, 1.0)

    body_top = 1.0 - _TITLE_H
    row_h = _BODY_H / n
    bar_h = row_h * ROW_FILL

    vals = [rolling_kwh.get(eid, 0.0) for eid in eids]
    max_val = max(vals) if any(v > 0 for v in vals) else 1.0

    ax.text(
        0.5,
        dlib.title_y(LAYOUT, body_top),
        CFG.title_right,
        ha="center",
        va="center",
        fontsize=FS_MAIN,
        fontweight="normal",
        fontfamily=_TITLE_FONT,
        transform=ax.transAxes,
    )

    for i in range(n):
        val = vals[i]
        row_top = body_top - i * row_h
        row_bot = row_top - row_h
        row_mid = (row_top + row_bot) / 2

        bar_frac = min(val / max_val, 1.0)
        bar_w = bar_frac * 0.84
        if bar_w > 0.005:
            ax.add_patch(
                mpatches.Rectangle(
                    (0.04, row_mid - bar_h / 2),
                    bar_w,
                    bar_h,
                    facecolor="white",
                    edgecolor="black",
                    linewidth=1.0,
                    transform=ax.transAxes,
                )
            )
        if val > 0:
            label_x = 0.04 + bar_w + 0.025
            if label_x > 0.95:
                ax.text(
                    0.04 + bar_w - 0.02,
                    row_mid,
                    f"{val:.2f}",
                    ha="right",
                    va="center",
                    fontsize=FS_SMALL,
                    transform=ax.transAxes,
                )
            else:
                ax.text(
                    label_x,
                    row_mid,
                    f"{val:.2f}",
                    ha="left",
                    va="center",
                    fontsize=FS_SMALL,
                    transform=ax.transAxes,
                )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    now = datetime.now(TZ)
    print(f"Fetching data from {CFG.ha_url} ...")
    power_history, rolling_kwh, total_kwh, transitions = load_data(now)
    print(f"  power_history: {power_history.shape}")
    print("Rendering ...")
    render(power_history, rolling_kwh, total_kwh, transitions, now, OUTPUT)


if __name__ == "__main__":
    main()
