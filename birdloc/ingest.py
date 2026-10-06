"""Ingestion: two SSE clients and the episode assembler.

**Events come from the `pending` stream, and only from it.** BirdNET-Go's
confirmed `detection` events -- and the `detections` rows they are written to --
are already de-duplicated to ONE camera per bird (the best-scoring source wins,
the rest are discarded), so an event built from them is single-camera by
construction and its blob is a fixed circle in front of that one camera. The
`pending` stream is the only surface that reports *every* camera currently
hearing a species (recon-birdnet-go-outputs §2a): a JSON list, re-emitted about
once a second, of `{species, scientificName, source, sourceID, firstDetected,
lastUpdated, audioCapturedAt, hitCount, status, bestModelID}` -- one entry per
(species, camera) alive in BirdNET-Go's buffer.

An **episode** is a species with a *tolerance-matched* start time: a pending
entry joins an open episode of the same species when its `firstDetected` is
within WINDOW_SECONDS of that episode's t0 or of the latest `firstDetected`
already attached to it; otherwise it opens a new one. Fixed
`firstDetected // WINDOW` buckets were wrong: BirdNET-Go's per-camera
`firstDetected` values for one bird differ by a second or two, so a bird heard
at 1788890108/109/113 landed in two different buckets and became two blobs
where three cameras had actually agreed (adversarial finding D).

An episode accumulates, per camera, the max `hitCount` seen and the max
song-band level seen. It closes CLOSE_AFTER_SECONDS after the last snapshot that
carried it, or MAX_EPISODE_SECONDS after it opened, and emits exactly one Event
with one Hit per camera. A closed (species, t) is remembered for
`closed_ttl` seconds and refuses to reopen: the store's uniqueness key is
(t, species), so a re-opened episode would be silently dropped on insert
(finding C).

`detection` events do three things: they teach source-id mappings, they carry
the confirmed confidence, and -- since 2026-09-08 -- they **gate emission**.

**The confirmation gate.** A pending entry is BirdNET-Go still deciding. Most of
what appears there is never confirmed: over a 300 s capture, 19 of 36 episodes
had no confirmed detection at or above threshold anywhere in their window, and
the `detection` stream itself is not threshold-filtered (10 of its 27 events
were below 0.7, down to 0.21 -- Blue Jay, Evening Grosbeak, Green-winged Teal).
Those unconfirmed and sub-threshold entries are exactly the phantom species that
reached the map. So an episode is emitted only when a `detection` event for the
same species, with `confidence >= CONFIRM_MIN_CONF`, on one of the episode's own
cameras, whose [beginTime, endTime] (else timestamp) overlaps
[t0 - W, last_first + W], has arrived. The confirmed confidence lands on that
one camera's Hit; the others keep `conf=None`. It is never a weight (owner:
confidence is a gate, not a weight).

An episode that closes unconfirmed is not dropped on the spot -- it waits in an
`awaiting` map for CONFIRM_GRACE_SECONDS, because the confirming detection can
land after the pending entries have stopped. Measured: the confirmation arrives
p50 14.2 s / p90 15.2 s / max 16.2 s after the episode's firstDetected. If it
never comes, the episode is discarded and counted in `unconfirmed_dropped`
(logged at INFO with species and cameras, so the impostor list is visible).
A short ring of recently confirmed detections is consulted when an episode
closes, so the gate does not depend on stream order (measured: no confirmation
preceded its own pending entries in that capture, but the margin was one tick).

Three identities exist per audio source (recon-birdnet-go-outputs §2c): the
credential-stripped RTSP URL, the hash id `rtsp_<8hex>`, and the display name
(= NVR channel name). The display name is this tool's key; `SourceMap`
maps the others onto it, learning from the `detection` event's source object and
from the `pending` entry's own (`sourceID`, `source`) pairing. birdnet.db is not
read at all: the pending stream teaches every name within seconds of start.

Until a hash is resolved, its soundlevel snapshots are held under the hash and
retro-attached the moment the name is learned; a camera with no *fresh* snapshot
(older than LEVEL_STALE_SECONDS, which is 2x the 10 s soundlevel cadence) gets
`band_db = None`, which means the fallback range and no band weighting at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from datetime import datetime

import httpx

from .bands import BandTable, band_level_db
from .config import Config
from .estimate import Event, Hit

log = logging.getLogger("birdloc.ingest")


# --------------------------------------------------------------------------
# helpers


def parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return None


async def sse_events(
    client: httpx.AsyncClient, url: str, on_connect: Callable[[], None] | None = None
) -> AsyncIterator[tuple[str, str]]:
    """Yield (event, data) pairs from a text/event-stream response."""
    async with client.stream("GET", url, timeout=httpx.Timeout(10.0, read=None)) as resp:
        resp.raise_for_status()
        if on_connect is not None:
            on_connect()
        name = "message"
        data: list[str] = []
        async for line in resp.aiter_lines():
            if line == "":
                if data:
                    yield name, "\n".join(data)
                name, data = "message", []
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "event":
                name = value
            elif field == "data":
                data.append(value)


# --------------------------------------------------------------------------
# source identity


class SourceMap:
    """hash / URL -> display name, learned as described in the module docstring."""

    def __init__(self) -> None:
        self.by_hash: dict[str, str] = {}
        self.by_uri: dict[str, str] = {}

    def learn_uri(self, uri: str, name: str) -> None:
        if uri and name:
            self.by_uri[uri] = name

    def learn_hash(self, source_hash: str, name: str) -> None:
        if source_hash and name and self.by_hash.get(source_hash) != name:
            self.by_hash[source_hash] = name
            log.info("source hash learned: %s -> %s", source_hash, name)

    def learn_from_source_obj(self, source: dict | None) -> str | None:
        """Take whatever a REST/SSE `source` object teaches, return the display name."""
        if not isinstance(source, dict):
            return None
        name = source.get("displayName") or None
        ident = source.get("id") or ""
        if name and ident:
            if ident.startswith("rtsp_"):
                self.learn_hash(ident, name)
            else:
                self.learn_uri(ident, name)
        if not name and ident:
            name = self.resolve(ident)
        return name

    def resolve(self, ident: str | None) -> str | None:
        if not ident:
            return None
        return self.by_hash.get(ident) or self.by_uri.get(ident) or None


# --------------------------------------------------------------------------
# soundlevels


class SoundLevels:
    """Latest 1/3-octave snapshot per source (keyed by hash, resolved to name).

    stale_seconds is 20 s -- twice the 10 s soundlevel cadence. A minute-old
    snapshot is a different bird, or no bird; using it as this episode's
    loudness would set a confident range from stale evidence.
    """

    def __init__(self, sources: SourceMap, stale_seconds: float = 20.0) -> None:
        self.sources = sources
        self.stale_seconds = stale_seconds
        self._by_hash: dict[str, tuple[float, dict]] = {}

    def update(self, payload: dict) -> None:
        ident = payload.get("source") or payload.get("name")
        bands = payload.get("octave_bands")
        if not ident or not isinstance(bands, dict):
            return
        t = parse_iso(payload.get("timestamp")) or time.time()
        self._by_hash[ident] = (t, bands)

    def snapshot_for(self, display_name: str) -> dict | None:
        now = time.time()
        for ident, (t, bands) in self._by_hash.items():
            if self.sources.resolve(ident) == display_name and now - t <= self.stale_seconds:
                return bands
        return None

    def level(self, display_name: str, species: str, table: BandTable) -> float | None:
        bands = self.snapshot_for(display_name)
        if bands is None:
            return None
        return band_level_db(bands, species, table)


# --------------------------------------------------------------------------
# episode assembler


class EpisodeAssembler:
    """Build one Event per species episode from `pending` snapshots.

    `level_fn(camera, species) -> float | None` supplies the song-band level for
    a camera at the moment a snapshot is seen; the max over the episode's life is
    kept, because the soundlevel stream only ticks every 10 s and a short episode
    may open before the loud snapshot arrives.
    """

    def __init__(
        self,
        cfg: Config,
        emit: Callable[[Event], None],
        level_fn: Callable[[str, str], float | None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.emit = emit
        self.level_fn = level_fn or (lambda camera, species: None)
        self._open: dict[tuple[str, float], dict] = {}
        # (species, emitted t) -> wall time it closed. Guards against reopening
        # an episode whose (t, species) row already exists (finding C).
        self._closed: dict[tuple[str, float], float] = {}
        # Episodes that closed without a confirmation yet: held here for
        # cfg.confirm_grace_seconds, then discarded.
        self._awaiting: list[dict] = []
        # Recently confirmed detections, so the gate is order-independent:
        # (wall, species, camera, conf, det_start, det_end).
        self._recent: deque[tuple[float, str, str, float, float, float]] = deque()
        self.emitted = 0
        self.multi_camera = 0
        self.reopen_refused = 0
        self.closed = 0
        self.unconfirmed_dropped = 0
        self.confirmations_seen = 0
        self.confirmations_below_threshold = 0

    @property
    def closed_ttl(self) -> float:
        return max(self.cfg.max_episode_seconds, 30.0) * 2.0

    # -- state ------------------------------------------------------------
    @property
    def open_count(self) -> int:
        return len(self._open)

    @property
    def awaiting_count(self) -> int:
        return len(self._awaiting)

    def _expire_closed(self, now: float) -> None:
        for key, when in list(self._closed.items()):
            if now - when > self.closed_ttl:
                del self._closed[key]

    def _match(self, species: str, first_detected: float) -> tuple[str, float] | None:
        """The open episode this firstDetected belongs to, by tolerance."""
        w = self.cfg.window_seconds
        best: tuple[str, float] | None = None
        best_gap = None
        for key, ep in self._open.items():
            if key[0] != species:
                continue
            gap = min(
                abs(first_detected - ep["t"]),
                abs(first_detected - ep["last_first"]),
            )
            # Chaining is bounded: a drip of firstDetected values W apart must not
            # merge past the episode's own lifetime (re-review defect 2).
            if abs(first_detected - ep["t"]) > self.cfg.max_episode_seconds:
                continue
            if gap <= w and (best_gap is None or gap < best_gap):
                best, best_gap = key, gap
        return best

    # -- input ------------------------------------------------------------
    def observe(self, species: str, sci_name: str, camera: str, hit_count: int,
                first_detected: float, now: float | None = None) -> None:
        """Fold one `pending` entry into its episode."""
        if not species or not camera:
            return
        now = time.time() if now is None else now
        self._expire_closed(now)
        key = self._match(species, first_detected)
        if key is None:
            # Refuse to reopen anything that already closed within W of this
            # firstDetected -- exact-key matching missed the case where a later,
            # higher firstDetected opens a new episode and a lower one then drags
            # its t onto an already-stored key (re-review defect 1).
            w = self.cfg.window_seconds
            if any(sp == species and abs(first_detected - tc) <= w
                   for (sp, tc) in self._closed):
                self.reopen_refused += 1
                return
            ep = {
                "species": species,
                "sci": sci_name or "",
                "t": first_detected,
                "last_first": first_detected,
                "opened": now,
                "last": now,
                "cams": {},
                # The confirmation gate: no episode is emitted while this is
                # False. Set only by confirm(), never by a pending entry.
                "confirmed": False,
            }
            key = (species, first_detected)
            self._open[key] = ep
        else:
            ep = self._open[key]
        ep["last"] = now
        ep["sci"] = ep["sci"] or sci_name or ""
        ep["t"] = min(ep["t"], first_detected)
        ep["last_first"] = max(ep["last_first"], first_detected)

        rec = ep["cams"].setdefault(camera, {"hits": 0, "band_db": None, "conf": None})
        rec["hits"] = max(rec["hits"], int(hit_count or 1))
        level = self.level_fn(camera, species)
        if level is not None:
            rec["band_db"] = level if rec["band_db"] is None else max(rec["band_db"], level)

    # -- the confirmation gate --------------------------------------------
    def _overlaps(self, ep: dict, det_start: float, det_end: float) -> bool:
        """Does a detection's [begin, end] touch this episode's window?"""
        w = self.cfg.window_seconds
        return det_end >= ep["t"] - w and det_start <= ep["last_first"] + w

    def _apply(self, ep: dict, camera: str, conf: float) -> None:
        rec = ep["cams"][camera]
        rec["conf"] = conf if rec["conf"] is None else max(rec["conf"], conf)
        ep["confirmed"] = True

    def confirm(
        self,
        species: str,
        camera: str,
        conf: float,
        det_start: float | None = None,
        det_end: float | None = None,
        now: float | None = None,
    ) -> bool:
        """A BirdNET-Go `detection` event: the only thing that unlocks emission.

        Below `confirm_min_conf` it is not a confirmation at all -- the SSE
        detection stream carries sub-threshold rows the database never keeps --
        and it is counted, not applied. At or above it, the confidence lands on
        the matching camera of every open or awaiting episode of that species
        whose window the detection overlaps, and those episodes become
        emittable. Awaiting ones are emitted immediately.
        """
        now = time.time() if now is None else now
        if not species or not camera or conf is None:
            return False
        self.confirmations_seen += 1
        if float(conf) < self.cfg.confirm_min_conf:
            self.confirmations_below_threshold += 1
            return False
        conf = float(conf)
        # A detection with no time at all cannot be placed in a window; it
        # confirms any live episode of that species on that camera.
        untimed = det_start is None and det_end is None
        if det_start is None:
            det_start = now
        if det_end is None:
            det_end = det_start

        def hits_episode(ep: dict) -> bool:
            return (ep["species"] == species and camera in ep["cams"]
                    and (untimed or self._overlaps(ep, det_start, det_end)))

        self._recent.append((now, species, camera, conf, det_start, det_end))
        self._prune_recent(now)

        matched = False
        for ep in self._open.values():
            if hits_episode(ep):
                self._apply(ep, camera, conf)
                matched = True
        for ep in list(self._awaiting):
            if hits_episode(ep):
                self._apply(ep, camera, conf)
                self._awaiting.remove(ep)
                self._emit_episode(ep)
                matched = True
        return matched

    def attach_confidence(
        self, species: str, camera: str, conf: float, t: float | None = None
    ) -> bool:
        """Back-compatible alias: confirm a detection known only by one instant.

        Kept because the confidence and the gate are the same event; a caller
        with a single timestamp rather than a begin/end pair lands here.
        """
        return self.confirm(species, camera, conf, t, t, now=None)

    def _prune_recent(self, now: float) -> None:
        ttl = self.cfg.confirm_ring_seconds
        while self._recent and now - self._recent[0][0] > ttl:
            self._recent.popleft()

    def _confirm_from_ring(self, ep: dict, now: float) -> bool:
        """Order independence: a confirmation may predate its pending entries."""
        self._prune_recent(now)
        found = False
        for _, species, camera, conf, det_start, det_end in self._recent:
            if species == ep["species"] and camera in ep["cams"] \
                    and self._overlaps(ep, det_start, det_end):
                self._apply(ep, camera, conf)
                found = True
        return found

    # -- output -----------------------------------------------------------
    def tick(self, now: float | None = None, force: bool = False) -> None:
        """Close episodes past their limits; expire the awaiting queue."""
        now = time.time() if now is None else now
        self._expire_closed(now)
        self._prune_recent(now)
        for key in list(self._open):
            ep = self._open[key]
            aged_out = now - ep["opened"] >= self.cfg.max_episode_seconds
            absent = now - ep["last"] >= self.cfg.close_after_seconds
            if force or aged_out or absent:
                self._close(key, now=now, capped=aged_out and not absent and not force)
        self._expire_awaiting(now, force=force)

    def _expire_awaiting(self, now: float, force: bool = False) -> None:
        for ep in list(self._awaiting):
            if not force and now - ep["closed_at"] < self.cfg.confirm_grace_seconds:
                continue
            self._awaiting.remove(ep)
            self.unconfirmed_dropped += 1
            log.info(
                "episode dropped, BirdNET-Go never confirmed it at >=%.2f: %s t=%s on %s",
                self.cfg.confirm_min_conf, ep["species"], ep["t"],
                ", ".join(sorted(ep["cams"])) or "-",
            )

    def _close(self, key: tuple[str, float], now: float | None = None,
               capped: bool = False) -> None:
        now = time.time() if now is None else now
        ep = self._open.pop(key)
        if not ep["cams"]:
            return
        self._closed[(ep["species"], ep["t"])] = now
        self._closed[key] = now  # the key it opened under, if t was lowered since
        if capped:
            log.info("episode capped at %.0fs: %s t=%s", self.cfg.max_episode_seconds,
                     ep["species"], ep["t"])
        self.closed += 1
        ep["closed_at"] = now
        if not ep["confirmed"]:
            self._confirm_from_ring(ep, now)
        if ep["confirmed"]:
            self._emit_episode(ep)
        else:
            # Not an impostor yet: the confirming detection routinely lands a
            # few seconds after the pending entries stop. Hold it for the grace
            # period; _expire_awaiting decides.
            self._awaiting.append(ep)

    def _emit_episode(self, ep: dict) -> None:
        hits = tuple(
            Hit(camera=name, hits=rec["hits"], band_db=rec["band_db"], conf=rec["conf"])
            for name, rec in sorted(ep["cams"].items())
        )
        if not hits:
            return
        self.emitted += 1
        if len(hits) > 1:
            self.multi_camera += 1
        self.emit(Event(species=ep["species"], sci_name=ep["sci"], t=ep["t"], hits=hits))


# --------------------------------------------------------------------------
# the running ingestor


class Ingestor:
    def __init__(self, cfg: Config, bands: BandTable, on_event: Callable[[Event], None]) -> None:
        self.cfg = cfg
        self.bands = bands
        self.on_event = on_event
        self.sources = SourceMap()
        self.levels = SoundLevels(self.sources, cfg.level_stale_seconds)
        self.assembler = EpisodeAssembler(cfg, self._emit, self._level)
        self.connected = {"detections": False, "soundlevels": False}
        self.counts = {"detections": 0, "pending": 0, "soundlevels": 0, "events": 0,
                       "pending_bad_items": 0}
        # events seen per stream, used only to decide whether a connection was
        # productive enough to reset its reconnect backoff
        self._progress = {"detections": 0, "soundlevels": 0}
        self.unknown_sources: dict[str, int] = {}

    def _level(self, camera: str, species: str) -> float | None:
        return self.levels.level(camera, species, self.bands)

    def _emit(self, event: Event) -> None:
        self.counts["events"] += 1
        self.on_event(event)

    def note_unknown_source(self, name: str) -> None:
        """A camera name the map does not know. Warn once, then just count."""
        if not name:
            return
        if name not in self.unknown_sources:
            self.unknown_sources[name] = 0
            log.warning("unknown camera name %r: not in cameras.yaml, its "
                        "contribution is dropped", name)
        self.unknown_sources[name] += 1

    # -- stats for /health -------------------------------------------------
    @property
    def stats(self) -> dict:
        return {
            "episodes_open": self.assembler.open_count,
            "episodes_closed": self.assembler.closed,
            "episodes_awaiting": self.assembler.awaiting_count,
            "episodes_emitted": self.assembler.emitted,
            "unconfirmed_dropped": self.assembler.unconfirmed_dropped,
            "confirmations_seen": self.assembler.confirmations_seen,
            "confirmations_below_threshold": self.assembler.confirmations_below_threshold,
            "confirm_min_conf": self.cfg.confirm_min_conf,
            "confirm_grace_seconds": self.cfg.confirm_grace_seconds,
            "multi_camera_emitted": self.assembler.multi_camera,
            "reopen_refused": self.assembler.reopen_refused,
            "unknown_sources": sorted(self.unknown_sources),
        }

    # -- streams ----------------------------------------------------------
    def _handle_detection(self, payload: dict) -> None:
        """Learn identity; confirm the episode this detection belongs to.

        Never a source of events: BirdNET-Go has already collapsed the confirmed
        detection to one camera by the time it reaches here. It is the *gate*:
        no episode is emitted until one of these arrives for it at or above
        CONFIRM_MIN_CONF (this stream is not threshold-filtered).
        """
        name = self.sources.learn_from_source_obj(payload.get("source"))
        self.counts["detections"] += 1
        if not name:
            return
        species = payload.get("commonName") or payload.get("scientificName") or ""
        conf = payload.get("confidence")
        ts = parse_iso(payload.get("timestamp"))
        begin = parse_iso(payload.get("beginTime")) or ts
        end = parse_iso(payload.get("endTime")) or begin
        if species and conf is not None:
            # confirm() can now emit an episode inline (an awaiting one whose
            # confirmation just landed), which means the whole estimate/store
            # path runs on this coroutine. One bad blob must not take the
            # detections stream down with it -- that would stop the gate and
            # silence the map. The ticker's guard did this for the old path.
            try:
                self.assembler.confirm(species, name, float(conf), begin, end)
            except Exception as exc:
                log.warning("confirmation failed for %s on %s: %s", species, name, exc)

    def _handle_pending(self, payload: list, now: float | None = None) -> None:
        for item in payload or []:
            if not isinstance(item, dict):
                continue
            # One malformed entry must never cost the rest of the snapshot: the
            # other cameras in this list are exactly the multi-camera evidence
            # the tool exists to capture.
            try:
                self.counts["pending"] += 1
                camera = item.get("source") or self.sources.resolve(item.get("sourceID"))
                self.sources.learn_hash(item.get("sourceID") or "", item.get("source") or "")
                first = item.get("firstDetected")
                if not camera or first is None:
                    continue
                self.assembler.observe(
                    species=item.get("species") or item.get("scientificName") or "",
                    sci_name=item.get("scientificName") or "",
                    camera=camera,
                    hit_count=int(item.get("hitCount") or 1),
                    first_detected=float(first),
                    now=now,
                )
            except (TypeError, ValueError, KeyError) as exc:
                self.counts["pending_bad_items"] += 1
                log.warning("bad pending entry skipped: %s", exc)
                continue

    async def _run_detections(self, client: httpx.AsyncClient) -> None:
        url = f"{self.cfg.birdnet_url}/api/v2/detections/stream"
        def _up() -> None:
            self.connected["detections"] = True
        async for name, data in sse_events(client, url, _up):
            self._progress["detections"] += 1
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            if name == "detection" and isinstance(payload, dict):
                self._handle_detection(payload)
            elif name == "pending" and isinstance(payload, list):
                self._handle_pending(payload)

    async def _run_soundlevels(self, client: httpx.AsyncClient) -> None:
        url = f"{self.cfg.birdnet_url}/api/v2/soundlevels/stream"
        def _up() -> None:
            self.connected["soundlevels"] = True
        async for name, data in sse_events(client, url, _up):
            self._progress["soundlevels"] += 1
            if name != "soundlevel":
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                self.counts["soundlevels"] += 1
                self.levels.update(payload)

    async def _supervise(self, which: str, runner) -> None:
        backoff = 1.0
        while True:
            before = self._progress[which]
            try:
                async with httpx.AsyncClient() as client:
                    await runner(client)
                log.warning("%s stream ended", which)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s stream error: %s", which, exc)
            self.connected[which] = False
            productive = self._progress[which] > before
            if productive:
                # A connection that actually delivered events is not a failing
                # endpoint: a stream that drops hourly must not creep to a 30 s
                # reconnect and stay there (finding F).
                backoff = 1.0
            await asyncio.sleep(backoff)
            if not productive:
                backoff = min(backoff * 2, 30.0)

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                self.assembler.tick()
            except Exception as exc:  # a bad episode must not kill the loop
                log.warning("episode tick failed: %s", exc)

    async def run(self) -> None:
        await asyncio.gather(
            self._supervise("detections", self._run_detections),
            self._supervise("soundlevels", self._run_soundlevels),
            self._ticker(),
        )
