"""FastAPI app: the map page and its JSON/SSE surfaces."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time

from fastapi import FastAPI, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)

from .bands import load_bands
from .cameras import load_cameras
from .config import load_config
from .estimate import Event, estimate, thumb_anchor, thumb_radius
from .ingest import Ingestor
from .photos import PhotoCache
from .store import Calibration, Store

log = logging.getLogger("birdnet_loc.web")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

# 12 distinct hues, muted so blobs sit inside the map illustration instead of
# on it (terracotta, mustard, teal, sage, plum, slate), legible at 0.4-0.6 opacity.
PALETTE = [
    "#b5533c", "#c9a24a", "#3f7d7a", "#7d8b4a", "#7a4e6b", "#5b6e8c",
    "#d1773b", "#2f5f66", "#a3623b", "#8f6f9e", "#b8892e", "#c58a8a",
]


def species_color(species: str) -> str:
    """The page's `species_color`, verbatim: SHA-1 of the name, first byte."""
    digest = hashlib.sha1(species.encode("utf-8")).digest()
    return PALETTE[digest[0] % len(PALETTE)]

__all__ = ["PALETTE", "app", "species_color", "state"]


class App:
    """Wiring: ingest -> estimate -> store, plus the live subscriber fan-out."""

    def __init__(self) -> None:
        self.cfg = load_config()
        self.cameras = load_cameras(self.cfg.cameras_path)
        self.bands = load_bands()
        self.store = Store(self.cfg.db_path, self.cfg.history_seconds)
        self.calib = Calibration(
            self.store.conn,
            self.store.lock,
            min_n=self.cfg.calib_min_n,
            max_n=self.cfg.calib_max_n,
            pct=self.cfg.calib_percentile,
        )
        self.photos = PhotoCache(
            os.path.join(self.cfg.data_dir, "photos"),
            birdnet_url=self.cfg.birdnet_url,
        )
        self.ingestor = Ingestor(self.cfg, self.bands, self.handle_event)
        self.subscribers: set[asyncio.Queue] = set()
        self.started_at = time.time()
        self.loop: asyncio.AbstractEventLoop | None = None
        # Every emitted episode lands in exactly one of these three, so
        # episodes_emitted == blobs_stored + store_rejected + estimate_dropped.
        self.blobs_stored = 0
        self.store_rejected = 0
        self.estimate_dropped = 0

    def handle_event(self, event: Event) -> None:
        # Loudness calibration is fed by every contribution, including the ones
        # whose camera the map does not know: the reference level is a property
        # of the species, not of the geometry.
        for hit in event.hits:
            if self.cameras.get(hit.camera) is None:
                self.ingestor.note_unknown_source(hit.camera)
            self.calib.observe(event.species, hit.band_db, event.t)

        blob = estimate(event, self.cameras, self.cfg, self.calib.ref_db)
        if blob is None:
            self.estimate_dropped += 1
            log.warning("episode dropped, no known camera: %s t=%s (%s)",
                        event.species, event.t,
                        ", ".join(h.camera for h in event.hits))
            return
        if not self.store.add(blob):
            self.store_rejected += 1
            log.warning("store rejected blob (t, species) already present: %s t=%s",
                        blob.species, blob.t)
            return
        self.blobs_stored += 1
        # Ask for the thumbnail; the background worker does the network, this
        # path only touches an in-process queue (never blocks an episode).
        self.photos.enqueue(blob.sci_name, blob.species)
        payload = self.blob_json(blob)
        for q in list(self.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(payload)

    def blob_json(self, blob) -> dict:
        """The wire shape: the estimate, its colour, and where its photo goes.

        Thumbnail geometry is computed here rather than stored, so blobs read
        back from SQLite after a restart carry it too, and so a change to the
        anchor rule needs no migration.
        """
        d = blob.to_dict()
        d["color"] = species_color(blob.species)
        tr = thumb_radius(blob.radius_px)
        tx, ty, moved = thumb_anchor(
            blob.x_px, blob.y_px, blob.radius_px, self.cameras.image_size, tr
        )
        d["thumb_x_px"] = round(tx, 1)
        d["thumb_y_px"] = round(ty, 1)
        d["thumb_r_px"] = round(tr, 1)
        d["thumb_moved"] = moved
        d["photo_url"] = self.photos.url_for(blob.sci_name)
        return d


state = App()
app = FastAPI(title="birdnet-loc", docs_url=None, redoc_url=None)


@app.on_event("startup")
async def _startup() -> None:
    # History is this tool's own record only: the blobs it estimated and stored.
    # There is no backfill from BirdNET-Go's `detections` table -- those rows are
    # de-duplicated to one camera per bird and cannot describe an episode.
    state.loop = asyncio.get_running_loop()
    log.info("loaded %d stored blobs", state.store.load_recent())
    log.info("loaded %d calibration samples over %d species",
             state.calib.load(), state.calib.species_seen)
    # Every species already in the 24 h ring gets its thumbnail asked for once,
    # at start, on the worker -- not on the first page load.
    queued = 0
    for b in state.store.since(0.0):
        if state.photos.enqueue(b.sci_name, b.species):
            queued += 1
    log.info("queued %d species for photo fetch", queued)
    app.state.photo_task = asyncio.create_task(state.photos.worker())
    app.state.task = asyncio.create_task(state.ingestor.run())


@app.on_event("shutdown")
async def _shutdown() -> None:
    for name in ("task", "photo_task"):
        task = getattr(app.state, name, None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    state.store.close()


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "uptime_s": round(time.time() - state.started_at, 1),
            "blobs": len(state.store),
            "cameras": len(state.cameras),
            "streams": state.ingestor.connected,
            "counts": state.ingestor.counts,
            "sources_known": len(state.ingestor.sources.by_hash),
            "blobs_stored": state.blobs_stored,
            "store_rejected": state.store_rejected,
            "estimate_dropped": state.estimate_dropped,
            # The episode-lifecycle invariant. Every closed episode is emitted,
            # dropped unconfirmed, or still inside its confirmation grace:
            #   episodes_closed == episodes_emitted + unconfirmed_dropped
            #                      + episodes_awaiting
            # which is the documented `closed == emitted + unconfirmed_dropped`
            # once the grace queue has drained. The other invariant,
            # episodes_emitted == blobs_stored + store_rejected + estimate_dropped,
            # is read straight off the counters below.
            "episode_accounting_ok": (
                state.ingestor.assembler.closed
                == (state.ingestor.assembler.emitted
                    + state.ingestor.assembler.unconfirmed_dropped
                    + state.ingestor.assembler.awaiting_count)
            ),
            "photos": state.photos.counters,
            "calib": {
                "species_calibrated": state.calib.species_calibrated,
                "species_seen": state.calib.species_seen,
                "min_n": state.calib.min_n,
            },
            **state.ingestor.stats,
        }
    )


# HEAD as well as GET: this route serves an image, and a HEAD from a browser
# cache or a probe must not read as "no such route".
@app.api_route("/api/photo/{sci:path}", methods=["GET", "HEAD"])
def api_photo(sci: str) -> Response:
    """The cached 192x192 thumbnail, or 404 while the worker has not got to it.

    This route NEVER fetches: a miss is a miss and the page draws its
    species-colour placeholder instead.
    """
    data = state.photos.get(sci)
    if data is None:
        state.photos.enqueue(sci)      # queue only; the worker does the network
        return Response(status_code=404)
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "max-age=86400"},
    )


@app.get("/api/photos")
def api_photos() -> JSONResponse:
    return JSONResponse(state.photos.status_map())


@app.get("/api/cameras")
def api_cameras() -> JSONResponse:
    return JSONResponse(
        {
            "image_size": list(state.cameras.image_size),
            "scale_m_per_px": state.cameras.scale_m_per_px,
            "cameras": [
                {
                    "name": c.name,
                    "x_px": c.x_px,
                    "y_px": c.y_px,
                    "bearing": c.bearing,
                    "fov_deg": c.fov_deg,
                }
                for c in state.cameras
            ],
        }
    )


@app.get("/api/blobs")
def api_blobs(
    since: float = Query(0.0),
    species: str | None = Query(None),
) -> JSONResponse:
    blobs = state.store.since(since, species)
    return JSONResponse(
        {
            "since": since,
            "count": len(blobs),
            "blobs": [state.blob_json(b) for b in blobs],
        }
    )


@app.get("/api/events/stream")
async def api_stream(request: Request) -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    state.subscribers.add(queue)

    async def gen():
        try:
            yield "event: connected\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    return
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: blob\ndata: {json.dumps(payload)}\n\n"
        finally:
            state.subscribers.discard(queue)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/map.png")
def map_png() -> FileResponse:
    return FileResponse(state.cfg.map_png_path, media_type="image/png")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    w, h = state.cameras.image_size
    return HTMLResponse(PAGE.replace("__W__", str(w)).replace("__H__", str(h)))


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>birdnet-loc - yard map</title>
<style>
  :root { color-scheme: light; --bg:#efe6d2; --fg:#2d3a35; --dim:#6b7a70; --line:#cdbfa3; --panel:#f6efdf; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  header { display:flex; gap:.75rem; align-items:baseline; flex-wrap:wrap;
           padding:.6rem .9rem; border-bottom:1px solid var(--line); }
  h1 { font-size:1rem; margin:0; font-weight:600; letter-spacing:.02em; }
  .status { color:var(--dim); font-size:.8rem; }
  button { background:var(--panel); color:var(--fg); border:1px solid var(--line);
           border-radius:6px; padding:.3rem .7rem; font:inherit; cursor:pointer; }
  button.on { background:#3f7d7a; color:#f6efdf; border-color:#2f5f66; }
  main { display:flex; gap:1rem; padding:.9rem; align-items:flex-start; flex-wrap:wrap; }
  #wrap { flex:1 1 640px; min-width:320px; max-width:__W__px; }
  svg { width:100%; height:auto; display:block; background:#efe6d2; border:1px solid var(--line);
        border-radius:8px; }
  aside { flex:0 0 250px; }
  .legend { list-style:none; margin:0; padding:0; }
  .legend li { display:flex; gap:.5rem; align-items:center; padding:.15rem 0; cursor:pointer; }
  .legend li.off { opacity:.35; }
  .sw { width:.85rem; height:.85rem; border-radius:50%; flex:0 0 auto; }
  .count { color:var(--dim); margin-left:auto; font-variant-numeric:tabular-nums; }
  #detail { margin-top:1rem; padding:.6rem; border:1px solid var(--line); border-radius:8px;
            min-height:4rem; font-size:.85rem; }
  #detail h2 { font-size:.9rem; margin:0 0 .3rem; }
  #detail table { width:100%; border-collapse:collapse; }
  #detail td { padding:.1rem .25rem; color:var(--dim); }
  #detail td:first-child { color:var(--fg); }
  .cam { fill:#f6efdf; stroke:#2d3a35; stroke-width:1.5; }
  .camline { stroke:#2d3a35; stroke-width:2; opacity:.7; }
  .camlabel { fill:#2d3a35; font-size:11px; paint-order:stroke; stroke:#f6efdf; stroke-width:3;
              stroke-linejoin:round; }
  circle.blob { cursor:pointer; }
  circle.blob.sel { stroke:#2d3a35; stroke-width:2.5; }
  /* Thumbnails are labels, not measurements: always fully opaque, never part
     of the per-view alpha budget the blobs share. */
  g.thumb { cursor:pointer; }
  g.thumb text.initial { fill:#f6efdf; font-weight:700; text-anchor:middle;
                         dominant-baseline:central; }
  g.thumb .leader { stroke-width:1.5; opacity:.75; stroke-dasharray:3 3; }
  g.thumb .badge { fill:#2d3a35; stroke:#f6efdf; stroke-width:1; }
  g.thumb text.badgetext { fill:#f6efdf; font-size:10px; font-weight:700;
                           text-anchor:middle; dominant-baseline:central; }
</style></head>
<body>
<header>
  <h1>birdnet-loc</h1>
  <button data-range="900">last 15 min</button>
  <button data-range="3600">last hour</button>
  <button data-range="today">today</button>
  <button id="multionly">multi-camera only</button>
  <span class="status" id="status">loading...</span>
</header>
<main>
  <div id="wrap">
    <svg id="map" viewBox="0 0 __W__ __H__" preserveAspectRatio="xMidYMid meet">
      <image href="map.png" x="0" y="0" width="__W__" height="__H__"></image>
      <defs id="thumbdefs"></defs>
      <defs id="graddefs"></defs>
      <g id="blobs"></g>
      <g id="cams"></g>
      <g id="thumbs"></g>
    </svg>
  </div>
  <aside>
    <ul class="legend" id="legend"></ul>
    <div id="detail"><h2>Nothing selected</h2><div class="status">
      Click a circle for its contributing cameras.</div></div>
  </aside>
</main>
<script>
const SVGNS = "http://www.w3.org/2000/svg";
let blobs = [], cameras = [], hidden = new Set(), range = 3600, selected = null;
let multiOnly = false;
// Scientific names whose thumbnail the server already has. A blob fetched
// before its photo landed carries photo_url:null forever, so the page asks
// /api/photos periodically instead of re-fetching every blob.
let photoReady = new Set();
function photoUrl(b) {
  if (b.photo_url) return b.photo_url;
  if (b.sci_name && photoReady.has(b.sci_name))
    return "api/photo/" + encodeURIComponent(b.sci_name);
  return null;
}
const nCams = b => (b.n_cameras != null ? b.n_cameras : (b.cameras || []).length);

function rangeStart() {
  if (range === "today") { const d = new Date(); d.setHours(0,0,0,0); return d.getTime()/1000; }
  return Date.now()/1000 - range;
}
function fmt(t) { return new Date(t*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}); }

function visible() {
  const from = rangeStart();
  return blobs.filter(b => b.t >= from && !hidden.has(b.species)
                           && (!multiOnly || nCams(b) > 1));
}

// The server keeps 24 h; a page left open for days must not accumulate beyond
// that or the "held" count and the legend drift away from what the API holds.
function dropStale() {
  const cutoff = Date.now()/1000 - 86400;
  if (blobs.some(b => b.t < cutoff)) blobs = blobs.filter(b => b.t >= cutoff);
}

function drawCameras() {
  const g = document.getElementById("cams");
  g.textContent = "";
  for (const c of cameras) {
    const rad = c.bearing * Math.PI / 180;
    const L = 46;
    const x2 = c.x_px + Math.sin(rad)*L, y2 = c.y_px - Math.cos(rad)*L;
    const line = document.createElementNS(SVGNS, "line");
    line.setAttribute("x1", c.x_px); line.setAttribute("y1", c.y_px);
    line.setAttribute("x2", x2); line.setAttribute("y2", y2);
    line.setAttribute("class", "camline");
    g.appendChild(line);
    const dot = document.createElementNS(SVGNS, "circle");
    dot.setAttribute("cx", c.x_px); dot.setAttribute("cy", c.y_px);
    dot.setAttribute("r", 5); dot.setAttribute("class", "cam");
    dot.appendChild(title(c.name + " - bearing " + c.bearing + "°"));
    g.appendChild(dot);
    const label = document.createElementNS(SVGNS, "text");
    label.setAttribute("x", c.x_px + 8); label.setAttribute("y", c.y_px - 8);
    label.setAttribute("class", "camlabel");
    label.textContent = c.name;
    g.appendChild(label);
  }
}
function title(text) {
  const t = document.createElementNS(SVGNS, "title"); t.textContent = text; return t;
}

// Overlap merge. Two episodes of the SAME species merge when their centres are
// closer than the smaller of the two radii -- i.e. one centre sits well inside
// the other blob, not merely a rim that grazes. Single-linkage (union-find), so
// a chain of overlaps becomes one cluster. Blobs and thumbnails are drawn from
// this same clustering, so a badge of x3 always names exactly one circle.
function clusterBlobs(rows) {
  const bySpecies = new Map();
  for (const b of rows) {
    let list = bySpecies.get(b.species);
    if (!list) { list = []; bySpecies.set(b.species, list); }
    list.push(b);
  }
  const out = [];
  for (const list of bySpecies.values()) {
    const parent = list.map((_, i) => i);
    const find = (i) => { while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i]; } return i; };
    for (let i = 0; i < list.length; i++) {
      for (let j = i + 1; j < list.length; j++) {
        const dx = list[i].x_px - list[j].x_px, dy = list[i].y_px - list[j].y_px;
        const d = Math.sqrt(dx*dx + dy*dy);
        if (d < Math.min(list[i].radius_px, list[j].radius_px)) {
          const a = find(i), c = find(j);
          if (a !== c) parent[c] = a;
        }
      }
    }
    const groups = new Map();
    for (let i = 0; i < list.length; i++) {
      const r = find(i);
      let m = groups.get(r);
      if (!m) { m = []; groups.set(r, m); }
      m.push(list[i]);
    }
    for (const members of groups.values()) {
      members.sort((a, b) => b.t - a.t);          // newest first, everywhere
      let wsum = 0, xs = 0, ys = 0, rmax = 0, cmax = 0;
      for (const m of members) {
        const w = Math.max(m.confidence || 0, 0.001);   // never a zero-weight mean
        wsum += w; xs += w * m.x_px; ys += w * m.y_px;
        if (m.radius_px > rmax) rmax = m.radius_px;
        if ((m.confidence || 0) > cmax) cmax = m.confidence || 0;
      }
      out.push({
        species: members[0].species, color: members[0].color,
        members: members, newest: members[0], n: members.length,
        // Radius is the largest member's, never the sum: the cluster is one
        // bird heard repeatedly, not a bigger uncertainty.
        x: xs / wsum, y: ys / wsum, r: rmax, conf: cmax,
      });
    }
  }
  return out;
}

// Stable per-cluster identity: species plus the newest member's timestamp.
function clusterKey(cl) { return cl.species + "|" + cl.newest.t; }

// One radial gradient per species colour: opaque at the centre, gone at the
// rim, with most of the fall-off in the outer half so the core still reads as
// a located bird. The per-view alpha budget rides on fill-opacity, so the
// gradient carries the SHAPE of the fade and the budget carries its level.
function gradId(color) { return "blobfade" + color.replace("#", ""); }

function gradientDefs(clusters) {
  const defs = document.getElementById("graddefs");
  defs.textContent = "";
  const seen = new Set();
  for (const cl of clusters) {
    if (!cl.color || seen.has(cl.color)) continue;
    seen.add(cl.color);
    const gd = document.createElementNS(SVGNS, "radialGradient");
    gd.setAttribute("id", gradId(cl.color));
    for (const [off, op] of [["0%", 1], ["55%", 0.82], ["100%", 0]]) {
      const st = document.createElementNS(SVGNS, "stop");
      st.setAttribute("offset", off);
      st.setAttribute("stop-color", cl.color);
      st.setAttribute("stop-opacity", op);
      gd.appendChild(st);
    }
    defs.appendChild(gd);
  }
}

function draw() {
  const g = document.getElementById("blobs");
  g.textContent = "";
  dropStale();
  const rows = visible();
  const clusters = clusterBlobs(rows);
  gradientDefs(clusters);
  // Per-cluster alpha budget: a camera's field should read as a shade proportional to how many
  // blobs sit in it, never a solid disc. Budget ~6 "full" blobs of alpha per view. Counting
  // clusters (not raw blobs) means merging makes the view more legible, not dimmer.
  const perBlob = Math.min(0.35, Math.max(0.015, 6 / Math.max(clusters.length, 1)));
  for (const cl of clusters) {
    const b = cl.newest;
    const c = document.createElementNS(SVGNS, "circle");
    c.setAttribute("cx", cl.x.toFixed(1));
    c.setAttribute("cy", cl.y.toFixed(1));
    c.setAttribute("r", cl.r.toFixed(1));
    c.setAttribute("fill", "url(#" + gradId(cl.color) + ")");
    c.setAttribute("fill-opacity", (perBlob * (0.5 + cl.conf)).toFixed(3));
    c.setAttribute("class", "blob" + (selected === b ? " sel" : ""));
    c.appendChild(title(cl.species + "\\n" + fmt(b.t) + "\\n" +
      (cl.n > 1 ? cl.n + " overlapping episodes\\n" : "") +
      nCams(b) + " camera" + (nCams(b) === 1 ? "" : "s") + "\\n" +
      b.cameras.map(x => x.name + " x" + x.hits).join("\\n")));
    c.addEventListener("click", () => { selected = b; draw(); detail(b, cl); });
    g.appendChild(c);
  }
  drawThumbs(clusters);
  legend(rows);
  const multi = rows.filter(b => nCams(b) > 1).length;
  // The count is of episodes, not clusters; the cluster count is named only
  // when merging actually collapsed something.
  const merged = clusters.length < rows.length
    ? " (" + clusters.length + " clusters)" : "";
  document.getElementById("status").textContent =
    rows.length + " episodes" + merged + " (" + multi + " multi-camera) / " +
    blobs.length + " held";
}

// One thumbnail per cluster: dozens of single-camera episodes of one bird land
// on the same point, and dozens of stacked identical photos is a smear, not a
// label. The cluster carries a count badge instead.
function drawThumbs(clusters) {
  const defs = document.getElementById("thumbdefs");
  const g = document.getElementById("thumbs");
  defs.textContent = ""; g.textContent = "";

  let i = 0;
  for (const cl of clusters) {
    const b = cl.newest;
    // Server-side geometry (estimate.thumb_anchor): already pulled inside the
    // frame when the blob itself hangs off the map.
    const tx = (b.thumb_x_px != null) ? b.thumb_x_px : b.x_px;
    const ty = (b.thumb_y_px != null) ? b.thumb_y_px : b.y_px;
    const tr = (b.thumb_r_px != null) ? b.thumb_r_px
                                      : Math.min(28, Math.max(14, b.radius_px * 0.35));
    const gr = document.createElementNS(SVGNS, "g");
    gr.setAttribute("class", "thumb");

    if (b.thumb_moved) {
      // The true centre may be off-image; SVG clips the line at the frame.
      const ln = document.createElementNS(SVGNS, "line");
      ln.setAttribute("x1", tx.toFixed(1)); ln.setAttribute("y1", ty.toFixed(1));
      ln.setAttribute("x2", b.x_px.toFixed(1)); ln.setAttribute("y2", b.y_px.toFixed(1));
      ln.setAttribute("class", "leader"); ln.setAttribute("stroke", b.color);
      gr.appendChild(ln);
    }

    const url = photoUrl(b);
    if (url) {
      const id = "thumbclip" + (i++) + "-" +
                 clusterKey(cl).replace(/[^A-Za-z0-9]/g, "");
      const clip = document.createElementNS(SVGNS, "clipPath");
      clip.setAttribute("id", id);
      const cc = document.createElementNS(SVGNS, "circle");
      cc.setAttribute("cx", tx.toFixed(1)); cc.setAttribute("cy", ty.toFixed(1));
      cc.setAttribute("r", tr.toFixed(1));
      clip.appendChild(cc); defs.appendChild(clip);

      const img = document.createElementNS(SVGNS, "image");
      img.setAttribute("href", url);
      img.setAttribute("x", (tx - tr).toFixed(1));
      img.setAttribute("y", (ty - tr).toFixed(1));
      img.setAttribute("width", (tr * 2).toFixed(1));
      img.setAttribute("height", (tr * 2).toFixed(1));
      img.setAttribute("preserveAspectRatio", "xMidYMid slice");
      img.setAttribute("clip-path", "url(#" + id + ")");
      gr.appendChild(img);
    } else {
      // No photo yet (or none exists): the species colour with its initial.
      const disc = document.createElementNS(SVGNS, "circle");
      disc.setAttribute("cx", tx.toFixed(1)); disc.setAttribute("cy", ty.toFixed(1));
      disc.setAttribute("r", tr.toFixed(1)); disc.setAttribute("fill", b.color);
      gr.appendChild(disc);
      const letter = document.createElementNS(SVGNS, "text");
      letter.setAttribute("x", tx.toFixed(1)); letter.setAttribute("y", ty.toFixed(1));
      letter.setAttribute("class", "initial");
      letter.setAttribute("font-size", Math.round(tr * 1.1));
      letter.textContent = (b.species || "?").trim().charAt(0).toUpperCase();
      gr.appendChild(letter);
    }

    const ring = document.createElementNS(SVGNS, "circle");
    ring.setAttribute("cx", tx.toFixed(1)); ring.setAttribute("cy", ty.toFixed(1));
    ring.setAttribute("r", tr.toFixed(1));
    ring.setAttribute("fill", "none"); ring.setAttribute("stroke", b.color);
    ring.setAttribute("stroke-width", selected === b ? 3.5 : 2);
    gr.appendChild(ring);

    if (cl.n > 1) {
      const bx = tx + tr * 0.78, by = ty - tr * 0.78;
      const bg = document.createElementNS(SVGNS, "circle");
      bg.setAttribute("cx", bx.toFixed(1)); bg.setAttribute("cy", by.toFixed(1));
      bg.setAttribute("r", 8.5); bg.setAttribute("class", "badge");
      const bt = document.createElementNS(SVGNS, "text");
      bt.setAttribute("x", bx.toFixed(1)); bt.setAttribute("y", by.toFixed(1));
      bt.setAttribute("class", "badgetext");
      bt.textContent = "\u00d7" + cl.n;
      gr.append(bg, bt);
    }

    gr.appendChild(title(b.species + (b.sci_name ? "\\n" + b.sci_name : "") +
      "\\n" + cl.n + " episode" + (cl.n === 1 ? "" : "s") + " here" +
      "\\nnewest " + fmt(b.t) + (b.thumb_moved ? "\\n(estimate is off the map edge)" : "")));
    gr.addEventListener("click", () => { selected = b; draw(); detail(b, cl); });
    g.appendChild(gr);
  }
}

// The worker fetches photos in the background; poll for the ones that landed.
async function refreshPhotos() {
  try {
    const map = await (await fetch("api/photos")).json();
    const next = new Set();
    for (const [sci, ok] of Object.entries(map)) if (ok) next.add(sci);
    let changed = next.size !== photoReady.size;
    if (!changed) for (const s of next) if (!photoReady.has(s)) { changed = true; break; }
    photoReady = next;
    if (changed) draw();
  } catch (e) { /* a photo poll failure is not a page failure */ }
}

function legend(rows) {
  // One count per species = the episodes of it currently in view.
  // Only species actually in the current range: a legend of 40 species with
  // 38 zeroes is a list of everything the tool has ever heard, not a key to
  // what is on the map.
  const counts = new Map();
  for (const b of rows) counts.set(b.species, (counts.get(b.species)||0) + 1);
  for (const name of hidden) if (!counts.has(name)) counts.set(name, 0);
  const ul = document.getElementById("legend");
  ul.textContent = "";
  const names = [...counts.keys()].sort();
  for (const name of names) {
    const li = document.createElement("li");
    if (hidden.has(name)) li.className = "off";
    const sw = document.createElement("span");
    sw.className = "sw";
    const any = rows.find(b => b.species === name) || blobs.find(b => b.species === name);
    sw.style.background = any ? any.color : "#888";
    const label = document.createElement("span"); label.textContent = name;
    const n = document.createElement("span"); n.className = "count";
    n.textContent = counts.get(name) || 0;
    li.append(sw, label, n);
    li.addEventListener("click", () => {
      if (hidden.has(name)) hidden.delete(name); else hidden.add(name);
      draw();
    });
    ul.appendChild(li);
  }
}

function detail(b, cl) {
  const d = document.getElementById("detail");
  d.textContent = "";
  const h = document.createElement("h2"); h.textContent = b.species;
  const sub = document.createElement("div"); sub.className = "status";
  sub.textContent = (b.sci_name || "") + " - " + fmt(b.t) + " - " + nCams(b) +
    " camera" + (nCams(b) === 1 ? "" : "s") +
    " - estimate confidence " + b.confidence.toFixed(2) +
    (b.confirmed_conf == null ? ""
      : " - confirmed " + b.confirmed_conf.toFixed(2) + " on " + b.confirmed_by) +
    (b.clamped ? " - extends beyond the map (street side)" : "") +
    (b.degraded ? " - degraded (a camera was not on the map)" : "");
  const table = document.createElement("table");
  for (const c of b.cameras) {
    const tr = document.createElement("tr");
    for (const v of [c.name, c.hits + " hits",
                     c.band_db == null ? "no band" : c.band_db.toFixed(1) + " dB",
                     c.range_m == null ? "" : c.range_m.toFixed(1) + " m",
                     "w " + c.weight,
                     c.conf == null ? "" : "conf " + c.conf]) {
      const td = document.createElement("td"); td.textContent = v; tr.appendChild(td);
    }
    table.appendChild(tr);
  }
  d.append(h, sub, table);

  // A merged circle stands for several episodes: name them, newest first, so
  // the "x3" badge can be read back to actual times and cameras.
  if (cl && cl.n > 1) {
    const hm = document.createElement("h2");
    hm.textContent = cl.n + " merged episodes";
    const mt = document.createElement("table");
    const shown = cl.members.slice(0, 10);
    for (const m of shown) {
      const tr = document.createElement("tr");
      for (const v of [m.species, fmt(m.t),
                       (m.cameras || []).map(c => c.name).join(", ")]) {
        const td = document.createElement("td"); td.textContent = v; tr.appendChild(td);
      }
      mt.appendChild(tr);
    }
    d.append(hm, mt);
    if (cl.members.length > shown.length) {
      const more = document.createElement("div");
      more.className = "status";
      more.textContent = "…and " + (cl.members.length - shown.length) + " more";
      d.appendChild(more);
    }
  }
}

// Fetch only the selected range: since=0 pulled the full 24 h on every load
// (thousands of rows) to draw the last 15 minutes.
async function fetchBlobs() {
  const since = Math.floor(rangeStart());
  const res = await (await fetch("api/blobs?since=" + since)).json();
  const seen = new Set(blobs.map(b => b.species + "|" + b.t));
  for (const b of res.blobs) if (!seen.has(b.species + "|" + b.t)) blobs.push(b);
  dropStale();
  draw();
}

async function load() {
  const cam = await (await fetch("api/cameras")).json();
  cameras = cam.cameras; drawCameras();
  await refreshPhotos();
  await fetchBlobs();
  const es = new EventSource("api/events/stream");
  es.addEventListener("blob", ev => { blobs.push(JSON.parse(ev.data)); draw(); });
}

for (const btn of document.querySelectorAll("button[data-range]")) {
  btn.addEventListener("click", () => {
    const v = btn.dataset.range;
    range = (v === "today") ? "today" : parseInt(v, 10);
    for (const b of document.querySelectorAll("button[data-range]")) b.classList.remove("on");
    btn.classList.add("on");
    draw();
    fetchBlobs();   // a wider range needs rows the page has not fetched yet
  });
}
document.getElementById("multionly").addEventListener("click", (ev) => {
  multiOnly = !multiOnly;
  ev.currentTarget.classList.toggle("on", multiOnly);
  draw();
});
document.querySelector('button[data-range="3600"]').classList.add("on");
setInterval(draw, 30000);
setInterval(refreshPhotos, 30000);
load();
</script>
</body></html>
"""
