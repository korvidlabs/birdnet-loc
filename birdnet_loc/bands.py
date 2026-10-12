"""Species -> song band table, and the mapping onto BirdNET-Go's 1/3-octave keys.

BirdNET-Go's `soundlevel` SSE payload carries 30 ISO 1/3-octave bands keyed by a
formatted centre frequency ("250.0_Hz", "2.5_kHz", ... recon-birdnet-go-outputs
§2b). A species' song band (lo_hz, hi_hz) selects every band whose centre falls
inside [lo, hi]; if that selects nothing (a band narrower than the octave grid),
the single nearest centre is used so a species always has at least one key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import yaml

# The 30 centres BirdNET-Go emits, with their payload keys, in ascending order.
OCTAVE_BANDS: tuple[tuple[float, str], ...] = (
    (25.0, "25.0_Hz"),
    (31.5, "31.5_Hz"),
    (40.0, "40.0_Hz"),
    (50.0, "50.0_Hz"),
    (63.0, "63.0_Hz"),
    (80.0, "80.0_Hz"),
    (100.0, "100.0_Hz"),
    (125.0, "125.0_Hz"),
    (160.0, "160.0_Hz"),
    (200.0, "200.0_Hz"),
    (250.0, "250.0_Hz"),
    (315.0, "315.0_Hz"),
    (400.0, "400.0_Hz"),
    (500.0, "500.0_Hz"),
    (630.0, "630.0_Hz"),
    (800.0, "800.0_Hz"),
    (1000.0, "1.0_kHz"),
    (1250.0, "1.2_kHz"),
    (1600.0, "1.6_kHz"),
    (2000.0, "2.0_kHz"),
    (2500.0, "2.5_kHz"),
    (3150.0, "3.1_kHz"),
    (4000.0, "4.0_kHz"),
    (5000.0, "5.0_kHz"),
    (6300.0, "6.3_kHz"),
    (8000.0, "8.0_kHz"),
    (10000.0, "10.0_kHz"),
    (12500.0, "12.5_kHz"),
    (16000.0, "16.0_kHz"),
    (20000.0, "20.0_kHz"),
)

DEFAULT_BAND: tuple[float, float] = (2500.0, 6300.0)


def band_keys(lo_hz: float, hi_hz: float) -> list[str]:
    """1/3-octave payload keys covering [lo_hz, hi_hz]."""
    keys = [k for centre, k in OCTAVE_BANDS if lo_hz <= centre <= hi_hz]
    if keys:
        return keys
    mid = (lo_hz + hi_hz) / 2.0
    nearest = min(OCTAVE_BANDS, key=lambda cb: abs(cb[0] - mid))
    return [nearest[1]]


@dataclass(frozen=True)
class BandTable:
    default: tuple[float, float]
    species: dict[str, tuple[float, float]]

    def band_for(self, species: str) -> tuple[float, float]:
        return self.species.get(species, self.default)

    def keys_for(self, species: str) -> list[str]:
        lo, hi = self.band_for(species)
        return band_keys(lo, hi)


def _default_path() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bands.yaml")


def load_bands(path: str | None = None) -> BandTable:
    path = path or os.environ.get("BANDS_YAML") or _default_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return BandTable(default=DEFAULT_BAND, species={})
    d = doc.get("default") or list(DEFAULT_BAND)
    species = {
        str(name): (float(rng[0]), float(rng[1]))
        for name, rng in (doc.get("species") or {}).items()
    }
    return BandTable(default=(float(d[0]), float(d[1])), species=species)


def band_level_db(octave_bands: dict, species: str, table: BandTable) -> float | None:
    """Max dB across the species' song band in one soundlevel snapshot.

    Returns None when the snapshot carries none of the species' bands.
    """
    best: float | None = None
    for key in table.keys_for(species):
        entry = octave_bands.get(key)
        if not isinstance(entry, dict):
            continue
        value = entry.get("max_db")
        if value is None:
            continue
        value = float(value)
        best = value if best is None else max(best, value)
    return best
