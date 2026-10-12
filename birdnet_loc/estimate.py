"""The estimation model (bearing weights and the loudness range model), as a pure function.

    w_i = hits_i x band_i x dir_i

      hits_i : min(1.0, hitCount_i / HITS_FULL) -- how insistently camera i
               heard the species over the episode. hitCount comes from the
               `pending` stream, which is the only surface that reports every
               camera that heard a bird (the confirmed `detection` events are
               already de-duplicated by BirdNET-Go to one best-scoring source).
      band_i : 10^((max_db_i - min_j max_db_j)/20) over the species' song band,
               relative to the quietest contributing camera, capped at 10, and
               applied ONLY when every contributor has a fresh level (mixing a
               measured camera with an assumed-1.0 camera silently pretends the
               unmeasured one is the quietest, which it is not).
      dir_i  : 1.0 inside the camera's field, BEHIND_WEIGHT behind it, judged
               against the centroid of the OTHER cameras' range points -- a
               camera is never "behind" its own estimate by construction. With
               one or two contributors dir_i is floored at 1.0: with two
               cameras the estimate lies between them, so neither can honestly
               be called "behind" (opposed-pair penalty, adversarial finding E).

**Range, not just direction.** Loudness sets how far along a camera's bearing
the bird is placed:

    range_i = REF_RANGE_M * 10^((ref_db_species - band_db_i) / 20)

clamped to [MIN_RANGE_M, MAX_RANGE_M]; 6 dB quieter is twice as far.
`ref_db_species` is self-calibrated (store.Calibration): the 95th percentile of
that species' observed band_db, treated as "about REF_RANGE_M away". Until a
species has CALIB_MIN_N samples -- or when a camera has no fresh level --
FALLBACK_RANGE_M is used for that camera.

    point_i = camera_i + range_i along bearing_i
    centre  = weighted centroid of point_i        (multi camera)
            = point_i                             (single camera)
    radius  = weighted standard distance of point_i, clamped   (multi camera)
            = max(MIN_RADIUS_M, range_i / 2), clamped          (single camera:
              bearing uncertainty grows with range)

The centre is never moved: `clamped: true` (name kept for API compatibility; it means
*extends beyond the map*) flags such a circle -- an estimate off the east edge is
the street, and a half-drawn circle reads as a smaller blob than it is.

Confidence = 1 - 1/(1 + sum w). When a contributor was dropped because its name
is not in cameras.yaml the episode is `degraded`: it must not read as a
confident single-camera estimate, so confidence is capped at the single-camera
value of its best remaining contributor.

Amplitude is a coarse cue (AGC mics, 16 kHz sources, 10 s band windows): the
output is honestly a blob, never a point.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

from .cameras import CameraSet
from .config import Config

BAND_FACTOR_CAP = 10.0


@dataclass(frozen=True)
class Hit:
    """One camera hearing one species over an episode.

    `hits` is the max hitCount that camera reported while the episode was open;
    `conf` is the confirmed BirdNET confidence if a `detection` event for this
    (species, camera) arrived before the episode closed, else None (it never
    enters the weight -- it is display only).
    """

    camera: str
    hits: int = 1
    band_db: float | None = None
    conf: float | None = None


@dataclass(frozen=True)
class Event:
    """One species episode: every camera that heard it, in one object."""

    species: str
    sci_name: str
    t: float
    hits: tuple[Hit, ...] = ()


@dataclass(frozen=True)
class Blob:
    species: str
    sci_name: str
    t: float
    x_px: float
    y_px: float
    radius_px: float
    confidence: float
    cameras: list[dict] = field(default_factory=list)
    clamped: bool = False
    degraded: bool = False
    # The confirmation that let this episode be emitted at all: BirdNET-Go's own
    # confidence for this species at or above its threshold, and the camera it
    # confirmed on. Never None on a live blob (the gate refuses to emit without
    # one); None only on rows stored before the gate shipped.
    confirmed_conf: float | None = None
    confirmed_by: str | None = None

    @property
    def n_cameras(self) -> int:
        return len(self.cameras)

    def to_dict(self) -> dict:
        return {
            "species": self.species,
            "sci_name": self.sci_name,
            "t": self.t,
            "x_px": self.x_px,
            "y_px": self.y_px,
            "radius_px": self.radius_px,
            "confidence": self.confidence,
            "n_cameras": len(self.cameras),
            "clamped": self.clamped,
            "degraded": self.degraded,
            "confirmed_conf": self.confirmed_conf,
            "confirmed_by": self.confirmed_by,
            "cameras": self.cameras,
        }


def _band_factors(hits: list[Hit]) -> dict[str, float]:
    """band_i per hit, relative to the quietest camera.

    Only applied when EVERY contributor carries a level: a camera with no fresh
    snapshot has an unknown level, and treating it as neutral 1.0 while another
    camera is scaled up is not a comparison, it is a guess dressed as one
    (adversarial finding G). Capped at BAND_FACTOR_CAP so one loud snapshot
    cannot swamp the geometry.
    """
    levels = [h.band_db for h in hits]
    if not levels or any(level is None for level in levels):
        return {h.camera: 1.0 for h in hits}
    quietest = min(levels)  # type: ignore[type-var]
    return {
        h.camera: min(BAND_FACTOR_CAP, 10.0 ** ((h.band_db - quietest) / 20.0))
        for h in hits
    }


def hits_factor(hit_count: int, hits_full: float) -> float:
    """min(1.0, hitCount / HITS_FULL).

    hit_count 0 (or negative, or missing) is floored to 1, not to 0: presence in
    the `pending` snapshot IS one analysis-window match, and BirdNET-Go has been
    seen emitting an entry before its counter ticks. A 0 would zero the whole
    weight and silently delete a camera that genuinely heard the bird
    (the pending stream is the only multi-camera surface there is).
    """
    return min(1.0, max(int(hit_count or 0), 1) / max(hits_full, 1e-9))


def range_for(band_db: float | None, ref_db: float | None, cfg: Config) -> float:
    """Metres along a camera's bearing implied by how loud it heard the bird."""
    if band_db is None or ref_db is None:
        return cfg.fallback_range_m
    r = cfg.ref_range_m * (10.0 ** ((ref_db - band_db) / 20.0))
    if not math.isfinite(r):
        return cfg.max_range_m
    return min(max(r, cfg.min_range_m), cfg.max_range_m)


def estimate(
    event: Event,
    cameras: CameraSet,
    cfg: Config,
    ref_db_fn: Callable[[str], float | None] | None = None,
) -> Blob | None:
    """Locate one episode. Pure: no I/O, no clock, no global state.

    `ref_db_fn(species) -> dB | None` supplies the self-calibrated reference
    level; None (the default, or an uncalibrated species) means every camera
    falls back to FALLBACK_RANGE_M.

    Returns None when no hit names a camera present in cameras.yaml.
    """
    hits = [h for h in event.hits if cameras.get(h.camera) is not None]
    if not hits:
        return None
    degraded = len(hits) < len(event.hits)

    scale = cameras.scale_m_per_px
    band = _band_factors(hits)
    hf = {h.camera: hits_factor(h.hits, cfg.hits_full) for h in hits}
    ref_db = None if ref_db_fn is None else ref_db_fn(event.species)

    ranges: dict[str, float] = {}
    points: dict[str, tuple[float, float]] = {}
    for h in hits:
        cam = cameras.get(h.camera)
        ux, uy = cam.unit
        rng = range_for(h.band_db, ref_db, cfg)
        ranges[h.camera] = rng
        points[h.camera] = (cam.x_m + rng * ux, cam.y_m + rng * uy)

    pre_w = {h.camera: hf[h.camera] * band[h.camera] for h in hits}

    weights: dict[str, float] = {}
    dirs: dict[str, float] = {}
    for h in hits:
        cam = cameras.get(h.camera)
        # Judge direction against where the OTHER cameras put the bird: a
        # camera's own range point is in its field by construction, so
        # including it can only vote "in front". With <=2 contributors the
        # answer is always 1.0 (finding E).
        others = [o for o in hits if o.camera != h.camera]
        if len(hits) <= 2 or not others:
            d = 1.0
        else:
            tot = sum(pre_w[o.camera] for o in others) or 1.0
            ox = sum(pre_w[o.camera] * points[o.camera][0] for o in others) / tot
            oy = sum(pre_w[o.camera] * points[o.camera][1] for o in others) / tot
            d = 1.0 if cam.in_field(ox, oy) else cfg.behind_weight
        dirs[h.camera] = d
        weights[h.camera] = hf[h.camera] * band[h.camera] * d

    total = sum(weights.values())
    if total <= 0.0:
        return None

    if len(hits) == 1:
        cx_m, cy_m = points[hits[0].camera]
        radius_m = max(cfg.min_radius_m, ranges[hits[0].camera] / 2.0)
    else:
        cx_m = sum(weights[h.camera] * points[h.camera][0] for h in hits) / total
        cy_m = sum(weights[h.camera] * points[h.camera][1] for h in hits) / total
        var = sum(
            weights[h.camera]
            * ((points[h.camera][0] - cx_m) ** 2 + (points[h.camera][1] - cy_m) ** 2)
            for h in hits
        ) / total
        radius_m = math.sqrt(var)

    radius_m = min(max(radius_m, cfg.min_radius_m), cfg.max_radius_m)

    confidence = 1.0 - 1.0 / (1.0 + total)
    if degraded:
        # Losing a camera to an unknown name must never look like a clean
        # single-camera fix (finding H).
        confidence = min(confidence, 1.0 - 1.0 / (1.0 + max(weights.values())))

    x_px, y_px = cx_m / scale, cy_m / scale
    radius_px = radius_m / scale
    # Do NOT move the estimate: an east-facing camera's bird really is over the
    # street beyond the map, and clamping put the circle back on the camera
    # (re-review defect 6). The page lets it overflow; the flag says so.
    w_img, h_img = cameras.image_size
    was_clamped = (x_px - radius_px < 0 or y_px - radius_px < 0
                   or x_px + radius_px > w_img or y_px + radius_px > h_img)
    cx_clamped, cy_clamped = x_px, y_px

    contributors = [
        {
            "name": h.camera,
            "hits": int(h.hits),
            "conf": None if h.conf is None else round(float(h.conf), 4),
            "band_db": h.band_db,
            "range_m": round(ranges[h.camera], 2),
            "ref_db": None if ref_db is None else round(ref_db, 2),
            "weight": round(weights[h.camera], 4),
            "dir": dirs[h.camera],
        }
        for h in sorted(hits, key=lambda x: -weights[x.camera])
    ]

    # The confirming camera is the one the gate attached a confidence to; the
    # others carry conf=None by construction (ingest._apply). Max, not sum: two
    # cameras can both be confirmed for one episode.
    confirmed = [h for h in hits if h.conf is not None]
    best_conf = max(confirmed, key=lambda h: h.conf) if confirmed else None

    return Blob(
        species=event.species,
        sci_name=event.sci_name,
        t=event.t,
        x_px=cx_clamped,
        y_px=cy_clamped,
        radius_px=radius_px,
        confidence=confidence,
        cameras=contributors,
        clamped=was_clamped,
        degraded=degraded,
        confirmed_conf=None if best_conf is None else round(float(best_conf.conf), 4),
        confirmed_by=None if best_conf is None else best_conf.camera,
    )


THUMB_MIN_R_PX = 14.0
THUMB_MAX_R_PX = 28.0
THUMB_R_FRACTION = 0.35
THUMB_EDGE_PAD_PX = 4.0


def thumb_radius(radius_px: float) -> float:
    """Thumbnail radius for a blob of `radius_px`: clamp(14, 0.35 r, 28).

    A thumbnail is a label, not a measurement -- it tracks blob size loosely so
    a tight fix reads smaller than a vague one, but never shrinks below legible
    or grows big enough to hide the map.
    """
    return min(THUMB_MAX_R_PX, max(THUMB_MIN_R_PX, float(radius_px) * THUMB_R_FRACTION))


def thumb_anchor(
    x_px: float,
    y_px: float,
    radius_px: float,
    image_size: tuple[int, int],
    thumb_r: float | None = None,
) -> tuple[float, float, bool]:
    """Where to draw a blob's photo thumbnail. Pure.

    The BLOB is never moved (estimate() deliberately lets an east-camera circle
    hang over the street past the map's edge). Its thumbnail is: a picture
    clipped in half, or drawn entirely off-image, tells the reader nothing. When
    the blob centre is off the image or within `thumb_r + 4` px of an edge, the
    thumb is pulled to the nearest point inset that far from every edge and
    `moved` is True -- the page then draws a leader line back to the true
    centre, which SVG clips at the frame.

    Returns `(tx, ty, moved)`.
    """
    tr = thumb_radius(radius_px) if thumb_r is None else float(thumb_r)
    w, h = float(image_size[0]), float(image_size[1])
    inset = tr + THUMB_EDGE_PAD_PX
    tx = _inset_axis(float(x_px), inset, w)
    ty = _inset_axis(float(y_px), inset, h)
    moved = abs(tx - float(x_px)) > 1e-9 or abs(ty - float(y_px)) > 1e-9
    return tx, ty, moved


def _inset_axis(v: float, inset: float, extent: float) -> float:
    if 2.0 * inset >= extent:   # image narrower than the thumb needs: centre it
        return extent / 2.0
    return min(max(v, inset), extent - inset)


def _clamp_to_image(
    x_px: float, y_px: float, radius_px: float, image_size: tuple[int, int]
) -> tuple[float, float, bool]:
    """Keep the whole circle inside the map image; report whether it moved."""
    w, h = float(image_size[0]), float(image_size[1])
    cx = _clamp_axis(x_px, radius_px, w)
    cy = _clamp_axis(y_px, radius_px, h)
    moved = abs(cx - x_px) > 1e-9 or abs(cy - y_px) > 1e-9
    return cx, cy, moved


def _clamp_axis(v: float, radius: float, extent: float) -> float:
    if 2.0 * radius >= extent:      # blob wider than the map: centre it
        return extent / 2.0
    return min(max(v, radius), extent - radius)
