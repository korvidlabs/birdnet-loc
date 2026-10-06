# birdloc

**Where in the yard was that bird?** birdloc listens to a [BirdNET-Go](https://github.com/tphakala/birdnet-go)
instance that is fed by several microphones (security cameras, in the original deployment), works out which
cameras heard each call and how loudly, and draws one translucent blob per species episode over a top-down
map of your yard.

## Quick start

You need a running BirdNET-Go with at least two RTSP (or other) audio sources whose names you know, and a
north-up image of the area they cover.

1. Copy `example/map/` somewhere, replace `map.png` with your own image and edit `cameras.yaml`: one entry
   per BirdNET-Go source, with its pixel position, the compass bearing it faces, and the image scale in
   metres per pixel. The file's comments explain every key. Source names must match BirdNET-Go's `source`
   field exactly.
2. Run it:

       cp .env.example .env            # BIRDNET_URL, MAP_DIR, lat/lon, ...
       docker compose up -d --build
       open http://localhost:9522      # the live map

   or without Docker:

       uv sync --group dev && uv run pytest -q
       BIRDNET_URL=http://localhost:8080 MAP_DIR=./example/map DATA_DIR=/tmp/birdloc \
         uv run uvicorn birdloc.web:app --port 9522

Blobs appear as soon as BirdNET-Go confirms a species on one of the cameras that heard it. The range
model self-calibrates per species, so positions tighten over the first few days.

## Configuration

Everything is read from the environment (`birdloc/config.py`); `docker-compose.yml` lists each knob with
its default and the reason for it. The ones you will actually set:

| Variable | Default | Meaning |
| --- | --- | --- |
| `BIRDNET_URL` | `http://birdnet-go:8080` | Your BirdNET-Go base URL. |
| `MAP_DIR` | `/map` | Directory holding `map.png` and `cameras.yaml`. |
| `DATA_DIR` | `/data` | Writable: the SQLite store and photo cache. |
| `CONFIRM_MIN_CONF` | `0.7` | Confidence floor; keep it equal to BirdNET-Go's `birdnet.threshold`. |
| `TZ` | `America/Denver` | Timezone for the "today" window. |

The sections below describe how the tool actually works, in the order the data flows.

## How it works

**Events come from the `pending` SSE stream only.** Confirmed `detection` events (and the
`detections` table) are already de-duplicated by BirdNET-Go to one camera per bird, so they can
never describe a multi-camera event; `pending` snapshots list every camera hearing a species.
An episode is a species whose `firstDetected` is within `WINDOW_SECONDS` of an already-open
episode of that species (tolerance matching, not fixed buckets: one bird's per-camera
`firstDetected` values differ by a second or two). It closes after `CLOSE_AFTER_SECONDS` of
absence or `MAX_EPISODE_SECONDS`, and never reopens once closed.

**The confirmation gate.** An episode is emitted only once BirdNET-Go has *confirmed* that
species with `confidence >= CONFIRM_MIN_CONF` (0.7, = `birdnet.threshold`) on one of the
episode's own cameras, in a `detection` event overlapping the episode's window — the SSE
detection stream is not threshold-filtered, so the gate applies the threshold itself. An
episode that closes unconfirmed waits `CONFIRM_GRACE_SECONDS` (20 s) and is then discarded and
counted in `/health.unconfirmed_dropped`; the confirming confidence and camera reach the blob as
`confirmed_conf` / `confirmed_by`. Confidence is a gate, never a weight.

Each contributing camera is weighted `hits x band x dir`, and its **range** along the bearing
comes from loudness: `range = REF_RANGE_M x 10^((ref_db - band_db)/20)`, clamped to
`[MIN_RANGE_M, MAX_RANGE_M]`, where `ref_db` is the 95th percentile of that species' observed
band levels (self-calibrated, `calib` table, `CALIB_MIN_N` samples before it is trusted;
`FALLBACK_RANGE_M` until then).

**Species photos** are 192x192 centre-cropped thumbnails drawn in the middle of each blob
(one per species per 8 px cell, with a `xN` badge when episodes stack). Sources, in order:
BirdNET-Go's `GET /api/v2/media/image/<scientific name>`, then Wikipedia's REST
`page/summary` lead image (scientific name, then common name), the same way the author's
household e-ink boards fetch theirs. Fetching happens only on a background worker, never in a request;
results are cached as PNGs in `DATA_DIR/photos/<slug>.png` and misses are retried after 24 h.

**History is only what this tool itself recorded** in `birdloc.db` — there is no backfill, and
BirdNET-Go's own SQLite is never opened.

## Origins and licence

birdloc was built for one household's BirdNET-Go node, fed by the audio of its Reolink cameras. It is MIT licensed (see `LICENSE`).
The upstream BirdNET-Go project is unmodified and is not part of this repository.
