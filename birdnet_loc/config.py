"""Settings for birdnet-loc, read from the environment.

Every knob of the estimation model lives here so a deployment can be
retuned without editing code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass



def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else float(raw)


def _i(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else int(float(raw))


@dataclass(frozen=True)
class Config:
    # --- sources ---------------------------------------------------------
    # No BirdNET-Go SQLite path: the tool reads nothing from birdnet.db. Source
    # names are taught by the `pending` and `detection` streams within seconds
    # of start, so the read-only mount that used to back `seed_from_db` is gone.
    birdnet_url: str = "http://birdnet-go:8080"
    map_dir: str = "/map"
    data_dir: str = "/data"

    # --- episode assembly (pending stream) -------------------------------
    # An episode is a species heard on >=1 camera whose `firstDetected` lies
    # within window_seconds of the episode's own t0 (or of its most recent
    # attached firstDetected). It closes close_after_seconds after the last
    # pending snapshot that carried it, or max_episode_seconds after it opened.
    window_seconds: float = 4.0
    close_after_seconds: float = 5.0
    max_episode_seconds: float = 30.0

    # --- confirmation gate -----------------------------------------------
    # An episode is only ever emitted once BirdNET-Go has CONFIRMED that
    # species, at or above its own threshold, on one of the episode's cameras.
    # The SSE `detection` stream is NOT threshold-filtered -- a 300 s capture on
    # 2026-09-08 carried 27 detection events of which 10 were below 0.7 (min
    # 0.21: Blue Jay, Evening Grosbeak, Green-winged Teal ...). Those are the
    # phantom species that reached the map. confirm_min_conf is BirdNET-Go's
    # `birdnet.threshold` from config/config.yaml (0.7) and must track it.
    confirm_min_conf: float = 0.7
    # How long a closed-but-unconfirmed episode is held before it is discarded.
    # Measured over that capture: the confirming detection arrives p50 14.2 s /
    # p90 15.2 s / max 16.2 s after the episode's firstDetected, and the episode
    # itself closes >= ~6 s after t0, so 20 s (p90 + 5 s) of grace from close
    # covers the whole observed tail with room to spare.
    confirm_grace_seconds: float = 20.0
    # Ring of recently confirmed detections, consulted when an episode closes,
    # so a confirmation that lands before its own pending entries still counts.
    confirm_ring_seconds: float = 120.0

    # --- estimation model ---------------------------
    hits_full: float = 3.0
    behind_weight: float = 0.3
    # Loudness sets RANGE, not just which camera is nearer:
    #   range_i = ref_range_m * 10^((ref_db_species - band_db_i)/20)
    # i.e. 6 dB quieter is twice as far, clamped to [min_range_m, max_range_m].
    # ref_db_species is the 95th percentile of that species' observed band_db
    # (see store.Calibration), treated as "about ref_range_m away".
    ref_range_m: float = 3.0
    min_range_m: float = 2.0
    max_range_m: float = 20.0
    fallback_range_m: float = 6.0     # no level, stale level, or species not yet calibrated
    calib_min_n: int = 20             # samples before a species' p95 is trusted
    calib_max_n: int = 500            # rolling reservoir depth per species
    calib_percentile: float = 95.0
    min_radius_m: float = 4.0
    max_radius_m: float = 25.0
    # NOTE: OFFSET_M and SINGLE_CAM_RADIUS_M are gone. The fixed 
    # "push every camera D metres along its bearing, single camera gets a fixed
    # radius" geometry was replaced by the per-camera range model above; a
    # deployment that still sets those env vars is simply ignored.

    # --- levels ----------------------------------------------------------
    level_stale_seconds: float = 20.0

    # --- retention -------------------------------------------------------
    history_seconds: float = 24 * 3600.0


    @property
    def cameras_path(self) -> str:
        return os.path.join(self.map_dir, "cameras.yaml")

    @property
    def map_png_path(self) -> str:
        return os.path.join(self.map_dir, "map.png")

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "birdnet-loc.db")


def load_config() -> Config:
    return Config(
        birdnet_url=os.environ.get("BIRDNET_URL", "http://birdnet-go:8080").rstrip("/"),
        map_dir=os.environ.get("MAP_DIR", "/map"),
        data_dir=os.environ.get("DATA_DIR", "/data"),
        window_seconds=_f("WINDOW_SECONDS", 4.0),
        close_after_seconds=_f("CLOSE_AFTER_SECONDS", 5.0),
        max_episode_seconds=_f("MAX_EPISODE_SECONDS", 30.0),
        confirm_min_conf=_f("CONFIRM_MIN_CONF", 0.7),
        confirm_grace_seconds=_f("CONFIRM_GRACE_SECONDS", 20.0),
        confirm_ring_seconds=_f("CONFIRM_RING_SECONDS", 120.0),
        hits_full=_f("HITS_FULL", 3.0),
        behind_weight=_f("BEHIND_WEIGHT", 0.3),
        ref_range_m=_f("REF_RANGE_M", 3.0),
        min_range_m=_f("MIN_RANGE_M", 2.0),
        max_range_m=_f("MAX_RANGE_M", 20.0),
        fallback_range_m=_f("FALLBACK_RANGE_M", 6.0),
        calib_min_n=_i("CALIB_MIN_N", 20),
        calib_max_n=_i("CALIB_MAX_N", 500),
        calib_percentile=_f("CALIB_PERCENTILE", 95.0),
        min_radius_m=_f("MIN_RADIUS_M", 4.0),
        max_radius_m=_f("MAX_RADIUS_M", 25.0),
        level_stale_seconds=_f("LEVEL_STALE_SECONDS", 20.0),
        history_seconds=_f("HISTORY_SECONDS", 24 * 3600.0),
    )
