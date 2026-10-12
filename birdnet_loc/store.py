"""Blob storage and the loudness calibration reservoir.

`Store` is a 24 h in-memory deque plus an append-only SQLite history.
`Calibration` is the self-calibrating reference level per species: the 95th
percentile of the band levels that species has actually been heard at, which the
range model reads as "about REF_RANGE_M away" (estimate.py).
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from collections import deque

from .estimate import Blob

SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  t          REAL NOT NULL,
  species    TEXT NOT NULL,
  sci_name   TEXT,
  x_px       REAL NOT NULL,
  y_px       REAL NOT NULL,
  radius_px  REAL NOT NULL,
  confidence REAL NOT NULL,
  cameras    TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blobs_t ON blobs(t);
CREATE UNIQUE INDEX IF NOT EXISTS idx_blobs_event ON blobs(t, species);

CREATE TABLE IF NOT EXISTS calib (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  species  TEXT NOT NULL,
  band_db  REAL NOT NULL,
  t        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calib_species ON calib(species, id);
"""

# Columns added after the first ship; SQLite has no "ADD COLUMN IF NOT EXISTS".
MIGRATIONS = (
    ("blobs", "clamped", "ALTER TABLE blobs ADD COLUMN clamped INTEGER NOT NULL DEFAULT 0"),
    ("blobs", "degraded", "ALTER TABLE blobs ADD COLUMN degraded INTEGER NOT NULL DEFAULT 0"),
    # The confirmation that let the episode be emitted (ingest.py gate). NULL
    # only on rows written before the gate shipped.
    ("blobs", "confirmed_conf", "ALTER TABLE blobs ADD COLUMN confirmed_conf REAL"),
    ("blobs", "confirmed_by", "ALTER TABLE blobs ADD COLUMN confirmed_by TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)


class Store:
    """Append-only. Nothing is ever updated or deleted from `blobs`."""

    def __init__(self, db_path: str, history_seconds: float = 24 * 3600.0) -> None:
        self.db_path = db_path
        self.history_seconds = history_seconds
        self._lock = threading.Lock()
        self._ring: deque[Blob] = deque()
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        _migrate(self._conn)
        self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def close(self) -> None:
        with self._lock:
            self._conn.commit()   # flush batched calibration inserts
            self._conn.close()

    # -- writes -----------------------------------------------------------
    def add(self, blob: Blob) -> bool:
        """Record a blob. Returns False when (t, species) was already stored."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO blobs (t, species, sci_name, x_px, y_px, radius_px, confidence,"
                    " cameras, created_at, clamped, degraded, confirmed_conf, confirmed_by)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        blob.t,
                        blob.species,
                        blob.sci_name,
                        blob.x_px,
                        blob.y_px,
                        blob.radius_px,
                        blob.confidence,
                        json.dumps(blob.cameras),
                        time.time(),
                        int(blob.clamped),
                        int(blob.degraded),
                        blob.confirmed_conf,
                        blob.confirmed_by,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                return False
            self._ring.append(blob)
            self._prune_locked()
            return True

    def _prune_locked(self) -> None:
        """Drop blobs older than the retention window.

        Blobs do NOT arrive in `t` order -- an episode's t is its earliest
        `firstDetected`, and a long episode closes after a short one that
        started later -- so popping from the left until the head is fresh can
        strand an old blob behind a newer one forever. Scan instead.
        """
        cutoff = time.time() - self.history_seconds
        if not any(b.t < cutoff for b in self._ring):
            return
        self._ring = deque(b for b in self._ring if b.t >= cutoff)

    # -- reads ------------------------------------------------------------
    def since(self, since_t: float, species: str | None = None) -> list[Blob]:
        with self._lock:
            self._prune_locked()
            rows = [b for b in self._ring if b.t >= since_t]
        if species:
            rows = [b for b in rows if b.species == species]
        return sorted(rows, key=lambda b: b.t)

    def __len__(self) -> int:
        with self._lock:
            return len(self._ring)

    def load_recent(self) -> int:
        """Repopulate the in-memory ring from SQLite (restart continuity)."""
        cutoff = time.time() - self.history_seconds
        with self._lock:
            cur = self._conn.execute(
                "SELECT t, species, sci_name, x_px, y_px, radius_px, confidence, cameras,"
                " clamped, degraded, confirmed_conf, confirmed_by"
                " FROM blobs WHERE t >= ? ORDER BY t",
                (cutoff,),
            )
            self._ring = deque(
                Blob(
                    species=r[1],
                    sci_name=r[2] or "",
                    t=r[0],
                    x_px=r[3],
                    y_px=r[4],
                    radius_px=r[5],
                    confidence=r[6],
                    cameras=json.loads(r[7]),
                    clamped=bool(r[8]),
                    degraded=bool(r[9]),
                    confirmed_conf=r[10],
                    confirmed_by=r[11],
                )
                for r in cur
            )
            return len(self._ring)


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an unsorted list (values must be non-empty)."""
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(pct / 100.0 * len(ordered))))
    return ordered[rank - 1]


class Calibration:
    """Per-species reference band level, self-calibrated from what we hear.

    Every contribution's `band_db` is remembered (rolling `max_n` per species,
    persisted in `calib` so a restart does not start from zero). The reference
    is the `pct`-th percentile of that reservoir: the level the species reaches
    when it is close, which the range model anchors at REF_RANGE_M. Below
    `min_n` samples the species has no usable reference and every camera falls
    back to FALLBACK_RANGE_M.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        lock: threading.Lock | None = None,
        min_n: int = 20,
        max_n: int = 500,
        pct: float = 95.0,
    ) -> None:
        self._conn = conn
        self._lock = lock or threading.Lock()
        self.min_n = int(min_n)
        self.max_n = int(max_n)
        self.pct = float(pct)
        self._by_species: dict[str, deque[float]] = {}
        self._since_trim: dict[str, int] = {}

    def load(self) -> int:
        """Read the most recent max_n samples per species back into memory."""
        with self._lock:
            self._by_species = {}
            cur = self._conn.execute(
                "SELECT species, band_db FROM calib ORDER BY id"
            )
            for species, band_db in cur:
                dq = self._by_species.setdefault(species, deque(maxlen=self.max_n))
                dq.append(float(band_db))
            return sum(len(v) for v in self._by_species.values())

    def observe(self, species: str, band_db: float | None, t: float | None = None) -> None:
        if not species or band_db is None or not math.isfinite(float(band_db)):
            return
        band_db = float(band_db)
        with self._lock:
            dq = self._by_species.setdefault(species, deque(maxlen=self.max_n))
            dq.append(band_db)
            self._conn.execute(
                "INSERT INTO calib (species, band_db, t) VALUES (?,?,?)",
                (species, band_db, time.time() if t is None else float(t)),
            )
            n = self._since_trim.get(species, 0) + 1
            self._since_trim[species] = n
            # One fsync per 50 samples, not per sample (re-review defect 4); the
            # in-memory ring is authoritative meanwhile and a crash loses <=49
            # calibration samples per species, which is harmless.
            if n >= 50:
                self._since_trim[species] = 0
                self._conn.execute(
                    "DELETE FROM calib WHERE species = ? AND id NOT IN ("
                    " SELECT id FROM calib WHERE species = ? ORDER BY id DESC LIMIT ?)",
                    (species, species, self.max_n),
                )
                self._conn.commit()

    def ref_db(self, species: str) -> float | None:
        with self._lock:
            dq = self._by_species.get(species)
            if dq is None or len(dq) < self.min_n:
                return None
            return percentile(list(dq), self.pct)

    def samples(self, species: str) -> int:
        with self._lock:
            return len(self._by_species.get(species, ()))

    @property
    def species_calibrated(self) -> int:
        with self._lock:
            return sum(1 for dq in self._by_species.values() if len(dq) >= self.min_n)

    @property
    def species_seen(self) -> int:
        with self._lock:
            return len(self._by_species)
