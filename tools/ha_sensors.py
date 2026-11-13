#!/usr/bin/env python3
"""
ha_sensors.py - list Home Assistant energy and power sensors, paired by
device, as [[entities]] tables for dashboards/config.toml.

The energy cards need, per device, a cumulative energy sensor in kWh and
(optionally) a live power sensor in W. This finds both kinds, pairs them by
the HA device they belong to (via the template API; entity-ID prefixes as a
fallback), and prints suggested tables to paste into config.toml. Labels
are the device names: shorten them, the label column is 80 px.

The cards don't convert units, so sensors in Wh, MWh or kW are listed with
a warning and left out of the suggestions, as are energy sensors for
exported / returned energy (smart plugs often report both directions).

Usage: ha_sensors.py [--ha-url URL] [--key-file .env]
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

import requests

ENERGY_UNITS = {"kWh"}
POWER_UNITS = {"W"}
OTHER_ENERGY_UNITS = {"Wh", "MWh"}
OTHER_POWER_UNITS = {"kW"}
# Energy sent back rather than used, e.g. sensor.plug_energy_exported.
EXPORTED = re.compile(r"export|return")

# One template call returns every sensor's device: much cheaper than an
# entity-registry lookup per sensor, and needs no websocket client.
DEVICE_TEMPLATE = """
{%- set ns = namespace(rows=[]) -%}
{%- for s in states.sensor -%}
{%- set d = device_id(s.entity_id) -%}
{%- set ns.rows = ns.rows + [{"entity_id": s.entity_id, "device_id": d,
    "device": (device_attr(d, "name_by_user") or device_attr(d, "name")) if d else none}] -%}
{%- endfor -%}
{{ ns.rows | to_json }}
"""


@dataclass
class Sensor:
    entity_id: str
    name: str
    unit: str
    state: str
    device_id: str | None = None
    device: str | None = None

    @property
    def stem(self) -> str:
        """The entity ID minus its kind suffix: sensor.plug_power_2 -> plug."""
        base = self.entity_id.removeprefix("sensor.")
        return re.sub(r"_(energy|power)(_total)?(_\d+)?$", "", base)

    @property
    def label(self) -> str:
        return self.device or self.name


def classify(states: list[dict]) -> tuple[list[Sensor], list[Sensor], list[tuple[Sensor, str]]]:
    """Split sensor states into energy, power, and (skipped, reason) lists."""
    energy: list[Sensor] = []
    power: list[Sensor] = []
    skipped: list[tuple[Sensor, str]] = []
    for s in states:
        eid = s["entity_id"]
        if not eid.startswith("sensor."):
            continue
        a = s.get("attributes", {})
        unit = a.get("unit_of_measurement") or ""
        cls = a.get("device_class")
        sensor = Sensor(eid, a.get("friendly_name", eid), unit, s.get("state", ""))
        if cls == "energy" or unit in ENERGY_UNITS | OTHER_ENERGY_UNITS:
            if a.get("state_class") not in ("total", "total_increasing"):
                continue  # not cumulative: can't take daily differences
            if EXPORTED.search(eid):
                skipped.append((sensor, "exported/returned energy"))
            elif unit in ENERGY_UNITS:
                energy.append(sensor)
            else:
                skipped.append((sensor, f"unit {unit}, cards need kWh"))
        elif cls == "power" or unit in POWER_UNITS | OTHER_POWER_UNITS:
            if unit in POWER_UNITS:
                power.append(sensor)
            else:
                skipped.append((sensor, f"unit {unit}, cards need W"))
    return energy, power, skipped


def attach_devices(sensors: list[Sensor], devices: dict[str, dict]) -> None:
    for s in sensors:
        row = devices.get(s.entity_id)
        if row:
            s.device_id = row.get("device_id")
            s.device = row.get("device")


def pair(energy: list[Sensor], power: list[Sensor]) -> list[tuple[Sensor, Sensor | None]]:
    """Each energy sensor with its power sensor: the only one on the same HA
    device, else the one with the same entity-ID stem (on the same device if
    the energy sensor has one; one device can hold several plugs)."""
    out = []
    for e in sorted(energy, key=lambda s: (s.label.lower(), s.entity_id)):
        pool = [p for p in power if p.device_id == e.device_id] if e.device_id else power
        if e.device_id and len(pool) == 1:
            match: Sensor | None = pool[0]
        else:
            same = [p for p in pool if p.stem == e.stem]
            match = same[0] if len(same) == 1 else None
        out.append((e, match))
    return out


def toml_tables(pairs: list[tuple[Sensor, Sensor | None]]) -> str:
    blocks = []
    for e, p in pairs:
        lines = ["[[entities]]", f'energy = "{e.entity_id}"']
        if p:
            lines.append(f'power  = "{p.entity_id}"')
        lines.append(f'label  = "{e.label}"')
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def main() -> None:
    ap = argparse.ArgumentParser(description="List HA energy/power sensors as config.toml [[entities]].")
    ap.add_argument("--ha-url", default="http://homeassistant.local:8123")
    ap.add_argument("--key-file", type=Path, default=Path(".env"),
                    help="file holding a long-lived access token (default: %(default)s)")
    args = ap.parse_args()

    url = args.ha_url.rstrip("/")
    headers = {"Authorization": f"Bearer {args.key_file.read_text().strip()}"}
    r = requests.get(f"{url}/api/states", headers=headers, timeout=15)
    r.raise_for_status()
    energy, power, skipped = classify(r.json())

    try:
        t = requests.post(f"{url}/api/template", headers=headers,
                          json={"template": DEVICE_TEMPLATE}, timeout=30)
        t.raise_for_status()
        devices = {row["entity_id"]: row for row in json.loads(t.text)}
        attach_devices(energy + power + [s for s, _ in skipped], devices)
    except (requests.RequestException, ValueError) as exc:
        print(f"# Device lookup failed ({exc}); pairing by entity ID instead.\n")

    pairs = pair(energy, power)
    paired = {p.entity_id for _, p in pairs if p}

    print(f"# {len(energy)} energy sensors (kWh), {len(power)} power sensors (W)\n")
    for e, p in pairs:
        print(f"#   {e.label:<28} {e.entity_id:<45} {p.entity_id if p else '(no power sensor)'}")
    unpaired = [p for p in power if p.entity_id not in paired]
    if unpaired:
        print("#\n# Power sensors without an energy sensor (not usable on their own):")
        for p in unpaired:
            print(f"#   {p.label:<28} {p.entity_id}")
    if skipped:
        print("#\n# Skipped:")
        for s, why in skipped:
            print(f"#   {s.entity_id:<45} {why}")
    print()
    print(toml_tables(pairs))


if __name__ == "__main__":
    main()
