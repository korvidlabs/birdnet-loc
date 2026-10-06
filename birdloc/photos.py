"""Species photo thumbnails for the map page.

One 192x192 PNG per species, centre-cropped ("zoomed in") with
`ImageOps.fit(..., LANCZOS)`.

Source order per scientific name:

  1. BirdNET-Go's own media proxy, `GET {BIRDNET_URL}/api/v2/media/image/<sci>`
     (verified live 2026-09-08: 320x240 JPEG for `Cyanocitta cristata`). It is
     on the LAN, already rate-limited by BirdNET-Go, and covers every species
     the detector knows.
  2. Wikipedia REST `page/summary/<title>`, `originalimage` then
     `thumbnail.source`, with a descriptive User-Agent and a politeness sleep
     between requests.
     Tried with the scientific name first, then the common name.

**Fetching never happens in the request path.** `PhotoCache.enqueue()` is a
non-blocking put onto an `asyncio.Queue`; a single background worker
(`PhotoCache.worker()`) does the network. `/api/photo/<sci>` reads the cache and
404s if it is not there yet -- a page load must never wait on Wikipedia.

Failures are cached negatively and retried after `RETRY_SECONDS` (24 h): a
species Wikipedia has no lead image for must not be re-fetched on every episode.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger("birdloc.photos")

THUMB_SIZE = (192, 192)
WIKI_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
# Butterfly's convention: a named, versioned agent so Wikipedia can identify us.
USER_AGENT = "birdloc/1.0 (https://github.com/tphakala/birdnet-go; household deployment)"
TIMEOUT_SECONDS = 10.0
POLITENESS_SLEEP_SECONDS = 1.0
RETRY_SECONDS = 24 * 3600.0

# Fetcher contract: async (url, headers) -> raw body bytes, or None on any
# failure. Injectable so the tests never touch the network.
Fetcher = Callable[[str, dict], Awaitable[bytes | None]]

__all__ = ["RETRY_SECONDS", "THUMB_SIZE", "PhotoCache", "fit_square", "slug"]


def slug(name: str) -> str:
    """The on-disk slug for a species name."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def fit_square(raw: bytes, size: tuple[int, int] = THUMB_SIZE) -> bytes:
    """Centre-crop-and-scale `raw` to a square PNG .

    `ImageOps.fit` crops to the target aspect around the centre and then
    resamples -- a 320x240 BirdNET-Go JPEG becomes the middle 240x240 of the
    frame at 192x192, which is the "zoomed in" crop the owner asked for.
    Raises on an undecodable body; every caller treats that as a fetch miss.
    """
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")
    img = ImageOps.fit(img, size, Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


async def _httpx_fetcher(url: str, headers: dict) -> bytes | None:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=True) as c:
            resp = await c.get(url, headers=headers)
            if resp.status_code >= 400:
                log.info("photo fetch %s -> HTTP %d", url, resp.status_code)
                return None
            return resp.content
    except Exception as exc:  # noqa: BLE001 - a photo miss is routine, never fatal
        log.info("photo fetch %s failed: %r", url, exc)
        return None


class PhotoCache:
    """Disk + memory cache of 192x192 species thumbnails.

    `dir` is `DATA_DIR/photos`; it persists across restarts through the same
    bind mount `birdloc.db` lives in.
    """

    def __init__(
        self,
        dir: str,
        fetcher: Fetcher | None = None,
        *,
        birdnet_url: str = "",
        sleep: Callable[[float], Awaitable[None]] | None = None,
        now: Callable[[], float] = time.time,
        retry_seconds: float = RETRY_SECONDS,
    ) -> None:
        self.dir = Path(dir)
        self.fetcher: Fetcher = fetcher or _httpx_fetcher
        self.birdnet_url = (birdnet_url or "").rstrip("/")
        self._sleep = sleep or asyncio.sleep
        self._now = now
        self.retry_seconds = float(retry_seconds)
        # sci -> png bytes (populated lazily from disk; small, ~15 kB each)
        self._mem: dict[str, bytes] = {}
        # sci -> monotonic-ish wall time of the last failure
        self._failed: dict[str, float] = {}
        # sci -> common name hint for the Wikipedia fallback
        self._common: dict[str, str] = {}
        self._queued: set[str] = set()
        # Every scientific name we have ever been asked about, so the status map
        # is keyed by real names -- the on-disk files are keyed by slug and a
        # slug cannot be turned back into "Cyanocitta cristata".
        self._known: set[str] = set()
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.fetched = 0

    # -- paths / lookup ---------------------------------------------------
    def path_for(self, sci: str) -> Path:
        return self.dir / (slug(sci) + ".png")

    def get(self, sci: str) -> bytes | None:
        """Cached PNG, memory first then disk. Never fetches."""
        if not sci:
            return None
        hit = self._mem.get(sci)
        if hit is not None:
            return hit
        path = self.path_for(sci)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if not data:
            return None
        self._mem[sci] = data
        return data

    def has(self, sci: str) -> bool:
        return self.get(sci) is not None

    def url_for(self, sci: str) -> str | None:
        """`/api/photo/<sci>` once the bytes exist, else None (page draws a disc)."""
        if not sci or not self.has(sci):
            return None
        return "/api/photo/" + quote(sci, safe="")

    # -- queueing ---------------------------------------------------------
    def enqueue(self, sci: str, common: str = "") -> bool:
        """Ask the background worker for this species. Non-blocking, idempotent.

        Returns True when the species was actually added to the queue. Skips a
        species already cached, already queued, or inside its negative-cache
        retry window.
        """
        if not sci:
            return False
        self._known.add(sci)
        if common:
            self._common.setdefault(sci, common)
        if sci in self._queued or self.has(sci):
            return False
        failed_at = self._failed.get(sci)
        if failed_at is not None and self._now() - failed_at < self.retry_seconds:
            return False
        self._queued.add(sci)
        self.queue.put_nowait(sci)
        return True

    # -- the fetch itself -------------------------------------------------
    async def fetch_one(self, sci: str) -> bool:
        """Try every source in order; write the thumbnail on the first that
        decodes. Returns True when a new file was written."""
        raw = None
        source = ""
        if self.birdnet_url:
            url = f"{self.birdnet_url}/api/v2/media/image/{quote(sci, safe='')}"
            raw = await self.fetcher(url, {"User-Agent": USER_AGENT})
            source = "birdnet-go"
        if not raw:
            for title_source in (sci, self._common.get(sci, "")):
                if not title_source:
                    continue
                raw = await self._fetch_wiki(title_source)
                if raw:
                    source = f"wikipedia:{title_source}"
                    break
        if not raw:
            self._failed[sci] = self._now()
            log.info("photo miss for %s (no source had an image)", sci)
            return False

        try:
            png = fit_square(raw)
        except Exception as exc:  # noqa: BLE001
            self._failed[sci] = self._now()
            log.info("photo decode failed for %s from %s: %r", sci, source, exc)
            return False

        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.path_for(sci)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(png)
            os.replace(tmp, path)
        except OSError as exc:
            self._failed[sci] = self._now()
            log.warning("photo cache write failed for %s: %r", sci, exc)
            return False

        self._mem[sci] = png
        self._failed.pop(sci, None)
        self.fetched += 1
        log.info("photo cached %s from %s (%d bytes)", sci, source, len(png))
        return True

    async def _fetch_wiki(self, title_source: str) -> bytes | None:
        """Butterfly's `fetch_wiki_photo` shape: summary -> originalimage /
        thumbnail.source -> image bytes, with a politeness sleep between the
        two requests and the same UA header."""
        title = title_source.replace(" ", "_")
        headers = {"User-Agent": USER_AGENT}
        body = await self.fetcher(WIKI_SUMMARY_URL.format(title=title), headers)
        if not body:
            return None
        try:
            meta = json.loads(body)
        except ValueError:
            return None
        image_meta = meta.get("originalimage") or meta.get("thumbnail") or {}
        src = image_meta.get("source")
        if not src:
            return None
        await self._sleep(POLITENESS_SLEEP_SECONDS)
        return await self.fetcher(src, headers)

    # -- the worker -------------------------------------------------------
    async def worker(self) -> None:
        """Single consumer. One species at a time, forever; cancellation-safe."""
        while True:
            sci = await self.queue.get()
            try:
                await self.fetch_one(sci)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                log.warning("photo worker error for %s: %r", sci, exc)
            finally:
                self._queued.discard(sci)
                self.queue.task_done()

    async def drain(self) -> None:
        """Process everything queued right now, then return (tests only)."""
        while not self.queue.empty():
            sci = self.queue.get_nowait()
            try:
                await self.fetch_one(sci)
            finally:
                self._queued.discard(sci)
                self.queue.task_done()

    # -- reporting --------------------------------------------------------
    def status_map(self) -> dict[str, bool]:
        """Every species we know about -> whether its thumbnail is on hand."""
        names = self._known | set(self._mem) | set(self._failed) | set(self._queued)
        return {sci: self.has(sci) for sci in sorted(names)}

    @property
    def counters(self) -> dict[str, int]:
        # Keyed by slug on both sides: the memory dict is keyed by scientific
        # name and the files by slug, and "Cyanocitta cristata" must not count
        # twice against "Cyanocitta_cristata.png".
        cached = {slug(s) for s in self._mem}
        try:
            cached |= {p.stem for p in self.dir.glob("*.png")}
        except OSError:
            pass
        now = self._now()
        failed = sum(1 for t in self._failed.values() if now - t < self.retry_seconds)
        return {
            "cached": len(cached),
            "pending": self.queue.qsize(),
            "failed": failed,
            "fetched": self.fetched,
        }
