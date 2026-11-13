#!/usr/bin/env python3
"""
weekly_energy_dashboard.py - 800x480 monochrome energy dashboard for reTerminal eInk display.

Left panel (584px):
  label column (80px) + 504px data area
  top   - grid of solid-black horizontal bars (device rows x day columns)
  bottom - 7-day sparkline (504 bins x 20 min = exactly 504px)

Right panel (~202px):
  cumulative weekly energy per device (horizontal bar chart)

Entities, fonts, and titles come from config.toml, not this file - see
DASHBOARD_SPEC.md for the full config/CLI contract and the style
conventions (pixel-perfect rendering, no dithering, font choices) this
script follows.

Usage: python weekly_energy_dashboard.py [--config CONFIG.toml]
       [--ha-url HA_URL] [--key-file KEY_FILE] [-o OUTPUT.png]
"""

from __future__ import annotations

from datetime import date as Date
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
import numpy as np
import polars as pl

import dashboardlib as dlib

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CARD_NAME = "weekly_energy"

_parser = dlib.build_arg_parser(
    "Render the weekly energy dashboard PNG.",
    default_output=Path(__file__).parent / "weekly_energy_dashboard.png",
)
_args = _parser.parse_args()
CFG = dlib.load_dashboard_config(CARD_NAME, _args)

OUTPUT = CFG.output

_FONT, _TITLE_FONT = dlib.setup_fonts(CFG)

FS_MAIN  = CFG.title_font_size   # section titles
FS_DEV   = CFG.label_font_size_dev
FS_SMALL = CFG.label_font_size_small

dlib.setup_matplotlib(_FONT, FS_SMALL)

TZ        = CFG.tz
W, H, DPI = CFG.canvas_width, CFG.canvas_height, CFG.dpi
# At DPI=72: 1pt = 1px exactly. lw=1 is one physical pixel; no compensation needed.

DEVICES: dict[str, str] = {e.energy: e.label for e in CFG.entities}
POWER_MAP: dict[str, str | None] = {e.energy: e.power for e in CFG.entities}

NICE_SCALE = [0.1, 0.5, 1, 2, 5, 10, 15, 20]

# ---------------------------------------------------------------------------
# Layout constants (figure fractions, 800x480 canvas, origin = bottom-left)
# Shared geometry lives in dashboardlib.compute_layout(); see DASHBOARD_SPEC.md.
# ---------------------------------------------------------------------------

LAYOUT = dlib.compute_layout(W, H)

L_GRID, W_GRID, LABEL_F   = LAYOUT.l_grid, LAYOUT.w_grid, LAYOUT.label_f
L_SPARK, W_SPARK          = LAYOUT.l_spark, LAYOUT.w_spark
B_SPARK, H_SPARK          = LAYOUT.b_spark, LAYOUT.h_spark
B_GRID, H_GRID            = LAYOUT.b_grid, LAYOUT.h_grid
_DIV_X, L_RIGHT, W_RIGHT  = LAYOUT.div_x, LAYOUT.l_right, LAYOUT.w_right
_TITLE_H, _DAYLABEL_H, _BODY_H = LAYOUT.title_h, LAYOUT.daylabel_h, LAYOUT.body_h

ROW_FILL = CFG.row_fill

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_data(days_back: int = 6) -> tuple[pl.DataFrame, pl.DataFrame, dict[str, float]]:
    """
    Returns:
      daily_df      - [date (Date), entity_id (Utf8), kwh (Float64)]
      power_history - [ts (Datetime UTC), entity_id (Utf8), watts (Float64)]
      power_w       - {energy_entity_id: current watts}
    """
    now        = datetime.now(TZ)
    all_eids   = list(DEVICES.keys())
    power_eids = [p for p in POWER_MAP.values() if p]
    states     = dlib.fetch_states(CFG)

    # Daily energy: anchor each midnight, shift by one day for delta.
    anchor_rows: list[dict] = []
    for d in range(days_back + 1):
        midnight = (now - timedelta(days=d)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        values = dlib.fetch_first_values(CFG, all_eids, midnight)
        day_str = midnight.date().isoformat()
        for eid, val in values.items():
            anchor_rows.append({"date": day_str, "entity_id": eid, "anchor_value": val})
        print(f"  midnight {day_str}: {len(values)}/{len(all_eids)} sensors")

    # Synthetic tomorrow row = current state; dropped by null filter after shift.
    tomorrow_str = (now + timedelta(days=1)).date().isoformat()
    for eid in all_eids:
        try:
            anchor_rows.append({
                "date": tomorrow_str,
                "entity_id": eid,
                "anchor_value": float(states[eid]),
            })
        except (ValueError, KeyError):
            pass

    daily_df = (
        pl.DataFrame(anchor_rows)
        .with_columns(pl.col("date").str.to_date())
        .sort(["entity_id", "date"])
        .with_columns(
            pl.col("anchor_value").shift(-1).over("entity_id").alias("next_anchor")
        )
        .filter(pl.col("next_anchor").is_not_null())
        .with_columns(
            (pl.col("next_anchor") - pl.col("anchor_value"))
            .clip(lower_bound=0.0)
            .alias("kwh")
        )
        .select(["date", "entity_id", "kwh"])
    )

    # Power history: 7 daily midnight queries to avoid ~1079-record truncation.
    print("  Fetching 7-day power history for sparkline...")
    power_frames: list[pl.DataFrame] = []
    for d in range(7):
        midnight = (now - timedelta(days=d)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        df = dlib.fetch_power_history(CFG, power_eids, midnight)
        power_frames.append(df)
        print(f"    day -{d}: {len(df)} records")
    power_history = (
        pl.concat(power_frames)
        .unique(subset=["ts", "entity_id"])
        .sort("ts")
    )

    power_w: dict[str, float] = {}
    for eid, pid in POWER_MAP.items():
        try:
            power_w[eid] = float(states.get(pid, "0")) if pid else 0.0
        except ValueError:
            power_w[eid] = 0.0

    return daily_df, power_history, power_w


def make_sparkline_df(power_history: pl.DataFrame) -> pl.DataFrame:
    """Aggregate total household watts per 20-minute bin (UTC truncation)."""
    if power_history.is_empty():
        return pl.DataFrame({
            "bin": pl.Series([], dtype=pl.Datetime("us", "UTC")),
            "total_w": pl.Series([], dtype=pl.Float64),
        })
    return (
        power_history
        .sort("ts")
        .with_columns(pl.col("ts").dt.truncate("20m").alias("bin"))
        .group_by(["bin", "entity_id"])
        .agg(pl.col("watts").mean())
        .group_by("bin")
        .agg(pl.col("watts").sum().alias("total_w"))
        .sort("bin")
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(
    daily_df:      pl.DataFrame,
    power_history: pl.DataFrame,
    now:           datetime,
    output:        Path,
) -> None:
    eids   = list(DEVICES.keys())
    labels = [DEVICES[e] for e in eids]
    today  = now.date()

    pivot = (
        daily_df.sort("date")
        .pivot(values="kwh", index="date", on="entity_id", aggregate_function="sum")
        .fill_null(0.0)
    )
    all_days: list[Date] = pivot["date"].to_list()
    n_days = len(all_days)
    kwh_matrix = np.array([
        pivot[eid].to_list() if eid in pivot.columns else [0.0] * n_days
        for eid in eids
    ])

    weekly_kwh = {
        eid: float(daily_df.filter(pl.col("entity_id") == eid)["kwh"].sum())
        for eid in eids
    }

    spark_df = make_sparkline_df(power_history)

    # Sparkline x-axis: exactly 7 days starting at midnight 6 days ago.
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start     = today_midnight - timedelta(days=6)
    week_end       = week_start + timedelta(days=7)

    fig = plt.figure(figsize=(W / DPI, H / DPI), dpi=DPI, facecolor="white")

    ax_grid  = fig.add_axes((L_GRID,  B_GRID,  W_GRID,  H_GRID))
    ax_spark = fig.add_axes((L_SPARK, B_SPARK, W_SPARK, H_SPARK))
    ax_right = fig.add_axes((L_RIGHT, B_GRID,  W_RIGHT, H_GRID))

    _render_grid(ax_grid, kwh_matrix, all_days, labels, today)
    _render_sparkline(ax_spark, spark_df, now, week_start, week_end)
    _render_weekly_summary(ax_right, weekly_kwh, eids)

    # Vertical divider line between left panel and right panel.
    fig.add_artist(mlines.Line2D(
        [_DIV_X, _DIV_X], [B_SPARK, 0.995],
        transform=fig.transFigure, color="black", linewidth=1,
    ))

    # Total weekly energy: centered in the blank lower-right area
    # (right of sparkline, below weekly summary panel).
    total_kwh = sum(weekly_kwh.values())
    if total_kwh < 100:
        total_str = f"{total_kwh:.1f} kWh"
    else:
        total_str = f"{round(total_kwh):.0f} kWh"

    fs = dlib.fit_text_to_area(fig, total_str, W_RIGHT * W, B_GRID * H, _FONT)
    fig.text(
        L_RIGHT + W_RIGHT / 2,
        B_GRID / 2,
        total_str,
        ha="center", va="center",
        fontsize=fs, fontweight="bold", fontfamily=_FONT,
    )

    dlib.save_thresholded(fig, output, DPI)


def _render_grid(
    ax:         maxes.Axes,
    kwh_matrix: np.ndarray,
    all_days:   list[Date],
    labels:     list[str],
    today:      Date,
) -> None:
    n_devices, n_days = kwh_matrix.shape
    ax.set_xlim(0.0, 1.0)
    ax.set_ylim(0.0, 1.0)
    ax.axis("off")

    max_kwh   = float(kwh_matrix.max()) or 1.0
    scale_kwh = next(v for v in NICE_SCALE if v >= max_kwh)

    title_h    = _TITLE_H
    daylabel_h = _DAYLABEL_H
    body_top   = 1.0 - title_h
    row_h      = _BODY_H / n_devices

    label_w   = LABEL_F
    col_w     = (1.0 - label_w) / n_days
    pad_x     = col_w * 0.05
    bar_vfrac = ROW_FILL

    ax.text(0.5, dlib.title_y(LAYOUT, body_top), "Daily Usage",
            ha="center", va="center", fontsize=FS_MAIN, fontweight="normal",
            fontfamily=_TITLE_FONT, transform=ax.transAxes)

    # Device rows (top-down); no separator lines between rows or columns.
    for i, lbl in enumerate(labels):
        row_top = body_top - i * row_h
        row_bot = row_top - row_h
        row_mid = (row_top + row_bot) / 2

        ax.text(label_w - 0.012, row_mid, lbl,
                ha="right", va="center", fontsize=FS_DEV, fontfamily=_FONT,
                transform=ax.transAxes)

        for d in range(n_days):
            kwh = kwh_matrix[i, d]
            if kwh <= 0.0:
                continue
            bar_frac   = min(kwh / scale_kwh, 1.0)
            cell_left  = label_w + d * col_w + pad_x
            bar_w      = bar_frac * (col_w - 2 * pad_x)
            bar_h      = row_h * bar_vfrac
            bar_bottom = row_mid - bar_h / 2
            ax.add_patch(mpatches.Rectangle(
                (cell_left, bar_bottom), bar_w, bar_h,
                facecolor="black", edgecolor="none",
                transform=ax.transAxes,
            ))

    # Thin rule above day-label strip.
    ax.plot([label_w, 1.0], [daylabel_h, daylabel_h],
            color="black", lw=1, transform=ax.transAxes)

    # Vertical rule separating label column from data area.
    ax.plot([label_w, label_w], [daylabel_h, body_top],
            color="black", lw=1, transform=ax.transAxes)

    # Day labels: lower portion of the daylabel strip.
    for d, day in enumerate(all_days):
        cx = label_w + d * col_w + col_w / 2
        cy = daylabel_h * 0.25
        is_today = (day == today)
        lbl = day.strftime("%a") + ("*" if is_today else "")
        ax.text(cx, cy, lbl,
                ha="center", va="center",
                fontsize=FS_SMALL,
                fontweight="bold" if is_today else "normal",
                transform=ax.transAxes)

    # Scale bar: upper portion of daylabel strip, spanning the first data column.
    # Whisker ends (|) + horizontal line + white-background label = "|-- 10 kWh --|"
    # Three plain plot() calls render as exactly 1px crisp lines with butt caps.
    bar_y  = daylabel_h * 0.72
    bar_x0 = label_w
    bar_x1 = label_w + col_w
    wh     = 5 / (H_GRID * H)   # whisker half-height in axes fraction (~5px each side)
    kw     = dict(color="black", lw=1, transform=ax.transAxes,
                  solid_capstyle="butt", zorder=2)
    ax.plot([bar_x0, bar_x1], [bar_y,      bar_y     ], **kw)
    ax.plot([bar_x0, bar_x0], [bar_y - wh, bar_y + wh], **kw)
    ax.plot([bar_x1, bar_x1], [bar_y - wh, bar_y + wh], **kw)
    ax.text(
        (bar_x0 + bar_x1) / 2, bar_y,
        f"{scale_kwh} kWh",
        ha="center", va="center", fontsize=FS_SMALL,
        transform=ax.transAxes,
        zorder=4,
        bbox=dict(facecolor="white", edgecolor="none", pad=2),
    )


def _render_sparkline(
    ax:         maxes.Axes,
    spark_df:   pl.DataFrame,
    now:        datetime,
    week_start: datetime,
    week_end:   datetime,
) -> None:
    ax.set_facecolor("white")
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_linewidth(1)

    ax.set_xlim(week_start, week_end)
    ax.margins(x=0)

    if not spark_df.is_empty():
        bins_raw = spark_df["bin"].to_list()
        watts    = spark_df["total_w"].to_list()
        bins_py: list[datetime] = [
            b.replace(tzinfo=timezone.utc).astimezone(TZ)
            if hasattr(b, "replace") else b
            for b in bins_raw
        ]
        ax.fill_between(bins_py, watts, 0, step="post", color="black", linewidth=0)
        peak = max(watts) if watts else 1.0
        ax.set_ylim(0, peak * 1.15)

    # Dashed day separators (every 72 bins = 1 day).
    for i in range(1, 7):
        ax.axvline(week_start + timedelta(days=i),
                   color="black", lw=1, linestyle=(0, (4, 4)))

    # Dotted current-time marker.
    ax.axvline(now, color="black", lw=1, linestyle=":")

    ax.xaxis.set_major_locator(mdates.DayLocator(tz=TZ))
    ax.tick_params(axis="x", bottom=False, labelbottom=False)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f}"))
    ax.yaxis.set_major_locator(mticker.MaxNLocator(nbins=2, integer=True))
    ax.tick_params(axis="y", labelsize=FS_SMALL, pad=1, width=1, length=3)
    ax.set_ylabel("W", fontsize=FS_SMALL, labelpad=2)


def _render_weekly_summary(
    ax:         maxes.Axes,
    weekly_kwh: dict[str, float],
    eids:       list[str],
) -> None:
    """Horizontal bar chart of cumulative weekly kWh; rows align with the grid body."""
    n = len(eids)
    ax.set_facecolor("white")
    ax.axis("off")
    ax.set_xlim(0.0, 1.0)
    ax.set_ylim(0.0, 1.0)

    title_h   = _TITLE_H
    body_top  = 1.0 - title_h
    row_h     = _BODY_H / n
    bar_hfrac = row_h * ROW_FILL

    vals    = [weekly_kwh.get(eid, 0.0) for eid in eids]
    max_val = max(vals) if any(v > 0 for v in vals) else 1.0

    ax.text(0.5, dlib.title_y(LAYOUT, body_top), "Weekly Usage",
            ha="center", va="center", fontsize=FS_MAIN, fontweight="normal",
            fontfamily=_TITLE_FONT, transform=ax.transAxes)

    for i in range(n):
        val     = vals[i]
        row_top = body_top - i * row_h
        row_bot = row_top - row_h
        row_mid = (row_top + row_bot) / 2

        bar_frac = min(val / max_val, 1.0)
        bar_w    = bar_frac * 0.84
        if bar_w > 0.005:
            ax.add_patch(mpatches.Rectangle(
                (0.04, row_mid - bar_hfrac / 2), bar_w, bar_hfrac,
                facecolor="white", edgecolor="black", linewidth=1.0,
                transform=ax.transAxes,
            ))
        if val > 0:
            label_x = 0.04 + bar_w + 0.025
            if label_x > 0.95:
                ax.text(0.04 + bar_w - 0.02, row_mid, f"{val:.1f}",
                        ha="right", va="center", fontsize=FS_SMALL, color="black",
                        transform=ax.transAxes)
            else:
                ax.text(label_x, row_mid, f"{val:.1f}",
                        ha="left", va="center", fontsize=FS_SMALL, color="black",
                        transform=ax.transAxes)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    now = datetime.now(TZ)
    print(f"Fetching data from {CFG.ha_url} ...")
    daily_df, power_history, _ = load_data()
    print(f"  daily_df: {daily_df.shape}, power_history: {power_history.shape}")
    print("Rendering ...")
    render(daily_df, power_history, now, OUTPUT)


if __name__ == "__main__":
    main()
