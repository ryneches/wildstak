"""
Shared library for reTerminal E1001 dashboard scripts.

Owns everything a dashboard script needs that isn't specific to its own
layout: the config/CLI contract, HA REST API fetch helpers, font
registration, the monochrome rendering setup, and the shared panel geometry
(title strip / day-label strip / body height, and title vertical-centering)
that keeps every card visually aligned when they rotate on the same device.

See DASHBOARD_SPEC.md for the design principles and conventions this
library encodes, and for the contract a new dashboard script must follow.
"""

from __future__ import annotations

import argparse
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
import matplotlib.pyplot as plt
from matplotlib import font_manager as _fm
from PIL import Image

# ---------------------------------------------------------------------------
# Config / CLI contract
#
# Every field below is either "deployment" (varies by machine/environment:
# ha_url, key_file) - CLI-overridable - or "design" (fonts, sizes, entities,
# row_fill) - config-file-only. See DASHBOARD_SPEC.md before adding a new
# CLI flag for something that belongs in config.toml instead.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entity:
    energy: str
    label: str
    power: str | None = None


@dataclass(frozen=True)
class DashboardConfig:
    card_name: str
    ha_url: str
    key_file: Path
    output: Path
    tz: ZoneInfo

    canvas_width: int
    canvas_height: int
    dpi: int
    row_fill: float

    label_font_path: Path
    label_font_size_dev: int
    label_font_size_small: int
    title_font_path: Path
    title_font_size: int

    entities: list[Entity]
    title_left: str
    title_right: str


def _existing_file(value: str) -> Path:
    path = Path(value)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"file not found: {path}")
    return path


def build_arg_parser(description: str, default_output: Path) -> argparse.ArgumentParser:
    """Shared CLI surface. Card-specific scripts may add extra arguments before parsing."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--config", type=_existing_file,
                         default=Path(__file__).parent / "config.toml",
                         help="Path to config.toml (default: %(default)s)")
    parser.add_argument("--ha-url", default=None,
                         help="Override config's [global] ha_url")
    parser.add_argument("--key-file", type=_existing_file, default=None,
                         help="Override config's [global] key_file")
    parser.add_argument("-o", "--output", type=Path, default=default_output,
                         help="Output PNG path (default: %(default)s)")
    return parser


def load_dashboard_config(card_name: str, args: argparse.Namespace) -> DashboardConfig:
    """Resolve [global] + [cards.<card_name>] + CLI overrides into one config object."""
    with open(args.config, "rb") as f:
        raw = tomllib.load(f)

    g = raw["global"]
    card = raw["cards"][card_name]
    config_dir = args.config.parent

    def _resolve_path(p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (config_dir / path)

    entities = [
        Entity(energy=e["energy"], label=e["label"], power=e.get("power"))
        for e in raw["entities"]
    ]

    return DashboardConfig(
        card_name=card_name,
        ha_url=(args.ha_url or g["ha_url"]).rstrip("/"),
        key_file=(args.key_file or _resolve_path(g["key_file"])),
        output=args.output,
        tz=ZoneInfo(g["timezone"]),
        canvas_width=g["canvas_width"],
        canvas_height=g["canvas_height"],
        dpi=g["dpi"],
        row_fill=card.get("row_fill", g["row_fill"]),
        label_font_path=_resolve_path(g["label_font"]["path"]),
        label_font_size_dev=g["label_font"]["size_dev"],
        label_font_size_small=g["label_font"]["size_small"],
        title_font_path=_resolve_path(g["title_font"]["path"]),
        title_font_size=g["title_font"]["size"],
        entities=entities,
        title_left=card["title_left"],
        title_right=card["title_right"],
    )


# ---------------------------------------------------------------------------
# Fonts and monochrome rendering setup
# ---------------------------------------------------------------------------


def setup_fonts(cfg: DashboardConfig) -> tuple[str, str]:
    """Register both fonts and return (label_font_family, title_font_family)."""
    _fm.fontManager.addfont(str(cfg.label_font_path))
    label_family = _fm.FontProperties(fname=str(cfg.label_font_path)).get_name()
    _fm.fontManager.addfont(str(cfg.title_font_path))
    title_family = _fm.FontProperties(fname=str(cfg.title_font_path)).get_name()
    return label_family, title_family


def setup_matplotlib(label_family: str, fs_small: int) -> None:
    """Disable all antialiasing - required for a clean hard threshold to 1-bit later."""
    matplotlib.rcParams.update({
        "font.family":       label_family,
        "font.size":         fs_small,
        "text.hinting":      "none",
        "text.antialiased":  False,
        "lines.antialiased": False,
        "patch.antialiased": False,
        "hatch.linewidth":   0.5,
    })


def save_thresholded(fig, output: Path, dpi: int) -> None:
    """Render, then hard-threshold to pure black/white. No grayscale ever ships."""
    fig.savefig(output, dpi=dpi, bbox_inches=None, facecolor="white")
    plt.close(fig)
    threshold = [255 if x >= 128 else 0 for x in range(256)]
    with Image.open(output) as img:
        img.convert("L").point(threshold).save(output)
    print(f"Saved {output}")


def save_thresholded_image(img: Image.Image, output: Path) -> None:
    """Same hard-threshold contract as save_thresholded, for a PIL-rendered
    (ImageDraw) card instead of a matplotlib one - no grayscale ever ships
    here either, even though PIL/FreeType renders our pixel-grid fonts
    without antialiasing in the first place (verified empirically; this is
    a safety net, not doing the real work)."""
    threshold = [255 if x >= 128 else 0 for x in range(256)]
    img.convert("L").point(threshold).save(output)
    print(f"Saved {output}")


def fit_text_to_area(
    fig, text: str, area_w_px: float, area_h_px: float,
    fontfamily: str, fontweight: str = "bold", fill: float = 0.65, ref_fs: int = 12,
) -> float:
    """
    Return a fontsize that renders `text` to fill `fill` of (area_w_px x
    area_h_px). Measures actual glyph extent at a reference size rather than
    guessing from character count, so it works for any string/font.
    """
    probe = fig.text(0.5, 0.5, text, fontsize=ref_fs, fontweight=fontweight,
                      fontfamily=fontfamily, ha="center", va="center")
    renderer = fig.canvas.get_renderer()
    bb = probe.get_window_extent(renderer)
    probe.remove()
    return ref_fs * min(area_w_px * fill / bb.width, area_h_px * fill / bb.height)


# ---------------------------------------------------------------------------
# Shared panel geometry
#
# Identical across every card so far, on purpose: it's what keeps a device
# row, the title band, and the divider line in the same place regardless of
# which card is on screen. See DASHBOARD_SPEC.md before changing these -
# a per-card override would break that alignment.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    w: int
    h: int
    label_px: int
    data_px: int
    l_grid: float
    w_grid: float
    label_f: float
    l_spark: float
    w_spark: float
    b_spark: float
    h_spark: float
    b_grid: float
    h_grid: float
    div_x: float
    l_right: float
    w_right: float
    title_h: float
    daylabel_h: float
    body_h: float


def compute_layout(canvas_width: int, canvas_height: int) -> Layout:
    w, h = canvas_width, canvas_height

    label_px = 80
    data_px = 504
    total_l = label_px + data_px  # 584

    l_grid = 0.001
    w_grid = total_l / w
    label_f = label_px / total_l
    l_spark = l_grid + label_px / w
    w_spark = data_px / w

    b_spark = 0.012
    h_spark = 0.175
    b_grid = b_spark + h_spark + 0.022
    h_grid = 1.0 - b_grid - 0.030  # extra top margin clears the panel's inset shadow

    div_x = l_grid + w_grid + 10 / w
    l_right = div_x + 1 / w
    w_right = 1.0 - l_right - 0.003

    title_h = 0.075
    daylabel_h = 0.100
    body_h = 1.0 - title_h - daylabel_h

    return Layout(
        w=w, h=h, label_px=label_px, data_px=data_px,
        l_grid=l_grid, w_grid=w_grid, label_f=label_f,
        l_spark=l_spark, w_spark=w_spark,
        b_spark=b_spark, h_spark=h_spark, b_grid=b_grid, h_grid=h_grid,
        div_x=div_x, l_right=l_right, w_right=w_right,
        title_h=title_h, daylabel_h=daylabel_h, body_h=body_h,
    )


def title_y(layout: Layout, body_top_axes_frac: float) -> float:
    """
    Axes-fraction y for a title centered between the absolute top of the
    screen and the top of the chart body - not just within the title_h
    strip, which starts below the extra top margin added for the panel's
    inset shadow and so would look off-center within the actual visible gap.

    body_top_axes_frac is the axes-fraction y where the chart body starts
    (e.g. 1.0 - layout.title_h for a panel whose title sits at the very top).
    """
    body_top_fig = layout.b_grid + body_top_axes_frac * layout.h_grid
    return ((1.0 + body_top_fig) / 2 - layout.b_grid) / layout.h_grid


# ---------------------------------------------------------------------------
# HA REST API helpers
#
# History queries are chunked per-midnight because a single query spanning
# more than ~1079 records gets silently truncated by Home Assistant.
# ---------------------------------------------------------------------------

from datetime import datetime  # noqa: E402

import polars as pl  # noqa: E402
import requests  # noqa: E402



def headers(cfg: DashboardConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {cfg.key_file.read_text().strip()}",
        "Content-Type": "application/json",
    }


def fetch_states(cfg: DashboardConfig) -> dict[str, str]:
    r = requests.get(f"{cfg.ha_url}/api/states", headers=headers(cfg), timeout=15)
    r.raise_for_status()
    return {s["entity_id"]: s["state"] for s in r.json()}


def fetch_first_values(
    cfg: DashboardConfig, entity_ids: list[str], start: datetime
) -> dict[str, float]:
    """Return the first valid float state for each sensor on or after `start`."""
    url = (
        f"{cfg.ha_url}/api/history/period/{start.isoformat()}"
        f"?filter_entity_id={','.join(entity_ids)}&minimal_response=true"
    )
    r = requests.get(url, headers=headers(cfg), timeout=60)
    r.raise_for_status()
    result: dict[str, float] = {}
    for series in r.json():
        if not series:
            continue
        eid = series[0].get("entity_id", "")
        if not eid:
            continue
        for rec in series:
            try:
                result[eid] = float(rec["state"])
                break
            except (ValueError, TypeError):
                continue
    return result


def fetch_power_history(
    cfg: DashboardConfig, power_eids: list[str], start: datetime
) -> pl.DataFrame:
    """Fetch power sensor history starting at `start`; return (ts, entity_id, watts)."""
    url = (
        f"{cfg.ha_url}/api/history/period/{start.isoformat()}"
        f"?filter_entity_id={','.join(power_eids)}&minimal_response=true"
    )
    r = requests.get(url, headers=headers(cfg), timeout=60)
    r.raise_for_status()
    rows: list[dict] = []
    for series in r.json():
        if not series:
            continue
        eid = series[0].get("entity_id", "")
        if not eid:
            continue
        for rec in series:
            ts_str = rec.get("last_changed") or rec.get("last_updated", "")
            if not ts_str:
                continue
            try:
                rows.append({
                    "ts": datetime.fromisoformat(ts_str.replace("Z", "+00:00")),
                    "entity_id": eid,
                    "watts": float(rec["state"]),
                })
            except (ValueError, TypeError):
                continue
    if not rows:
        return pl.DataFrame(schema={
            "ts": pl.Datetime("us", "UTC"),
            "entity_id": pl.Utf8,
            "watts": pl.Float64,
        })
    return pl.DataFrame(rows).with_columns(pl.col("ts").dt.replace_time_zone("UTC"))
