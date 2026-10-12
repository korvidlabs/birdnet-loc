"""Helper for maintaining `bands.yaml` by hand.

    python -m birdnet_loc.bands_tool check                      # validate and show band -> keys
    python -m birdnet_loc.bands_tool keys 1600 3100             # which 1/3-octave keys a range selects
    python -m birdnet_loc.bands_tool missing --birdnet-url URL  # species BirdNET-Go has heard, not in the table
    python -m birdnet_loc.bands_tool add "Blue Jay" 1600 3100   # append one entry

The table is never inferred: `missing` only reports what the instance has heard
and the default band those species currently fall back to. A human picks the
range (a field guide's "song frequency" line, or BirdNET-Go's spectrogram for a
clip) and records it with `add`.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections.abc import Callable

from .bands import DEFAULT_BAND, OCTAVE_BANDS, band_keys, load_bands

SUMMARY_PATH = "/api/v2/analytics/species/summary"

Fetch = Callable[[str], bytes]


def _default_bands_path() -> str:
    from .bands import _default_path

    return _default_path()


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "birdnet-loc bands_tool"})
    with urllib.request.urlopen(req, timeout=15) as res:  # noqa: S310 - operator-supplied URL
        return res.read()


def validate_range(lo: float, hi: float) -> list[str]:
    """Problems with a (lo, hi) band, in words. Empty means fine."""
    problems = []
    if lo <= 0 or hi <= 0:
        problems.append("frequencies must be positive")
    if lo >= hi:
        problems.append("lo must be below hi")
    lowest, highest = OCTAVE_BANDS[0][0], OCTAVE_BANDS[-1][0]
    if hi < lowest or lo > highest:
        problems.append(f"outside the {lowest:g}-{highest:g} Hz grid BirdNET-Go reports")
    return problems


def describe(lo: float, hi: float) -> str:
    keys = band_keys(lo, hi)
    exact = [k for c, k in OCTAVE_BANDS if lo <= c <= hi]
    note = "" if exact else "  (no centre inside the range: nearest band used)"
    return f"[{lo:g}, {hi:g}] -> {', '.join(keys)}{note}"


def cmd_check(path: str, out) -> int:
    table = load_bands(path)
    bad = 0
    print(f"default: {describe(*table.default)}", file=out)
    for name, (lo, hi) in sorted(table.species.items()):
        problems = validate_range(lo, hi)
        flag = "  !! " + "; ".join(problems) if problems else ""
        bad += bool(problems)
        print(f"{name}: {describe(lo, hi)}{flag}", file=out)
    print(f"{len(table.species)} species, {bad} with problems", file=out)
    return 1 if bad else 0


def cmd_keys(lo: float, hi: float, out) -> int:
    problems = validate_range(lo, hi)
    for p in problems:
        print(f"!! {p}", file=out)
    print(describe(lo, hi), file=out)
    return 1 if problems else 0


def species_summary(birdnet_url: str, fetch: Fetch = _http_get) -> list[dict]:
    body = fetch(birdnet_url.rstrip("/") + SUMMARY_PATH)
    rows = json.loads(body)
    if not isinstance(rows, list):
        raise SystemExit(f"unexpected response from {SUMMARY_PATH}: {type(rows).__name__}")
    return rows


def cmd_missing(path: str, birdnet_url: str, min_count: int, out, fetch: Fetch = _http_get) -> int:
    table = load_bands(path)
    rows = species_summary(birdnet_url, fetch)
    missing = [
        r for r in rows
        if r.get("common_name") and r["common_name"] not in table.species
        and int(r.get("count") or 0) >= min_count
    ]
    missing.sort(key=lambda r: -int(r.get("count") or 0))
    lo, hi = table.default
    if not missing:
        print(f"every species with >= {min_count} detections has a band", file=out)
        return 0
    print(f"# {len(missing)} species heard by {birdnet_url} without a band (default [{lo:g}, {hi:g}] applies).",
          file=out)
    print("# Look up each one's song range, then: python -m birdnet_loc.bands_tool add \"<name>\" <lo> <hi>",
          file=out)
    for r in missing:
        last = str(r.get("last_heard") or "")[:10]
        print(f"#   {r['common_name']}: {r.get('count')} detections, last {last}", file=out)
    return 0


def cmd_add(path: str, name: str, lo: float, hi: float, out) -> int:
    problems = validate_range(lo, hi)
    if problems:
        for p in problems:
            print(f"!! {p}", file=out)
        return 1
    table = load_bands(path)
    if name in table.species:
        print(f"!! {name} already has {describe(*table.species[name])}", file=out)
        return 1
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        text = f"default: [{DEFAULT_BAND[0]:g}, {DEFAULT_BAND[1]:g}]\nspecies:\n"
    if "\nspecies:" not in text and not text.startswith("species:"):
        text = text.rstrip("\n") + "\nspecies:\n"
    if not text.endswith("\n"):
        text += "\n"
    key = f'"{name}"' if ":" in name or name.startswith(("'", '"')) else name
    text += f"  {key}: [{lo:g}, {hi:g}]\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    check = load_bands(path).species.get(name)
    if check != (float(lo), float(hi)):
        print(f"!! wrote {path} but it does not read back; inspect the file", file=out)
        return 1
    print(f"{name}: {describe(lo, hi)}", file=out)
    return 0


def main(argv: list[str] | None = None, out=sys.stdout) -> int:
    ap = argparse.ArgumentParser(prog="bands_tool", description=__doc__.split("\n\n")[0])
    ap.add_argument("--bands", default=_default_bands_path(), help="path to bands.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="validate the table and show which 1/3-octave keys each band selects")
    k = sub.add_parser("keys", help="show which keys a frequency range selects")
    k.add_argument("lo", type=float)
    k.add_argument("hi", type=float)
    m = sub.add_parser("missing", help="species BirdNET-Go has heard that have no band yet")
    m.add_argument("--birdnet-url", required=True)
    m.add_argument("--min-count", type=int, default=1, help="ignore species heard fewer times")
    a = sub.add_parser("add", help="append one species to the table")
    a.add_argument("name")
    a.add_argument("lo", type=float)
    a.add_argument("hi", type=float)
    ns = ap.parse_args(argv)
    if ns.cmd == "check":
        return cmd_check(ns.bands, out)
    if ns.cmd == "keys":
        return cmd_keys(ns.lo, ns.hi, out)
    if ns.cmd == "missing":
        return cmd_missing(ns.bands, ns.birdnet_url, ns.min_count, out)
    return cmd_add(ns.bands, ns.name, ns.lo, ns.hi, out)


if __name__ == "__main__":
    raise SystemExit(main())
