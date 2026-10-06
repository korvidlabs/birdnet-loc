# AGENTS.md

Instructions for coding agents working in this repository. You start with zero context; everything
you need to know that is not derivable from the code is here or in `README.md`.

## What this is

birdloc estimates where a bird was from which BirdNET-Go audio sources heard it and how loudly, and
draws it as a blob over a top-down map. It is a single Python package (`birdloc/`) served by FastAPI,
with SQLite for state. There is no frontend build: the page is one HTML string in `birdloc/web.py`
with inline CSS and JS.

Operators follow `SETUP.md`. This file is about changing the code.

## Layout

```
birdloc/
  config.py     every setting, read from the environment; defaults documented inline
  ingest.py     the two SSE clients, source-name mapping, the episode assembler and confirmation gate
  estimate.py   the pure estimation model: bearing weights, loudness range, blob radius
  store.py      SQLite blobs ring (24 h) and the per-species loudness calibration reservoir
  bands.py      per-species song bands (Hz) from bands.yaml
  cameras.py    cameras.yaml loader: positions, bearings, scale
  photos.py     species thumbnail cache (BirdNET-Go media proxy, then Wikipedia), background worker
  web.py        the App wiring, the HTTP/SSE API and the map page
bands.yaml      the species band table (hand-maintained; see "Rules")
example/map/    a schematic yard and a commented cameras.yaml; the tests run against it
tests/          pytest; no network, no BirdNET-Go needed
```

## Commands

```
uv sync --group dev                 # environment (Python 3.12, uv; uv.lock is authoritative)
uv run pytest -q                    # the whole suite, ~1 s
uvx ruff check --select F,E9 birdloc tests
BIRDNET_URL=http://host:8080 MAP_DIR=./example/map DATA_DIR=/tmp/birdloc \
  uv run uvicorn birdloc.web:app --port 9522 --reload
docker compose up -d --build        # the container; .env supplies BIRDNET_URL, MAP_DIR, DATA_DIR
```

`tests/conftest.py` sets `MAP_DIR`, `DATA_DIR`, `BANDS_YAML` and `CONFIRM_MIN_CONF=0.75` before
`birdloc.web` is imported, because the App is built at import time.

## How the data flows

1. `ingest.py` holds two SSE connections to BirdNET-Go: `/api/v2/soundlevels/stream` (per-source band
   levels, every ~10 s) and `/api/v2/detections/stream` (`pending` snapshots about once a second, and
   `detection` events).
2. Episodes are assembled from `pending` snapshots only. A `pending` snapshot lists every source
   currently hearing a species; `detection` events are already de-duplicated by BirdNET-Go to one
   source per bird and can never describe a multi-source event.
3. An episode is emitted only once a `detection` event confirms that species at
   `confidence >= CONFIRM_MIN_CONF` on one of the episode's own sources. The detection stream is not
   threshold-filtered upstream, so the gate applies the threshold itself. Unconfirmed episodes are
   dropped after `CONFIRM_GRACE_SECONDS` and counted in `/health`.
4. `estimate.py` turns the episode into a blob: each source is weighted by hit count, band level
   and whether the bird is in front of it; its range along the bearing comes from loudness against
   the species' self-calibrated reference level (p95 of observed band levels, trusted after
   `CALIB_MIN_N` samples). Confidence is a gate, never a weight.
5. `store.py` keeps the blob; `web.py` fans it out to SSE subscribers and asks `photos.py` for the
   species thumbnail on a background worker.

## Rules

- **Settings live in `config.py` and are read from the environment.** A new knob gets a dataclass
  field with a comment saying what it does and why the default is what it is, a `load_config` line,
  and a line in `docker-compose.yml`. Nothing is configured by editing code.
- **`bands.yaml` grows by hand.** Unknown species fall back to `default`. Do not infer bands from
  data; add a row with the species' common name exactly as BirdNET-Go spells it.
- **No reads of BirdNET-Go's database.** Everything comes from its HTTP API and SSE streams. History
  is only what this tool itself recorded.
- **The request path never does network I/O.** Photo fetches and stream reads happen on background
  tasks. A request handler that awaits the network is a bug.
- **Tests make no network calls and need no BirdNET-Go.** Fetch tests hand the module a fake client.
  Keep it that way; the suite must run anywhere in about a second.
- **The threshold is floor-relative.** Tests and docs refer to `CONFIRM_MIN_CONF`, not to a literal.
  It must equal BirdNET-Go's `birdnet.threshold` on the instance being watched.
- **An honest empty state beats a fabricated number.** When there is no data, say so on the surface
  rather than guessing.
- **Keep the page one file.** The map page is a Python string; a `\n` inside a JS string literal
  becomes a real newline and breaks the script. `tests/test_page_js.py` parses the served script
  (with `node --check` when node is present). Run it after touching `PAGE`.
- **Secrets never enter the repo.** `.env` is ignored; `docker-compose.yml` and `.env.example` carry
  placeholders only.

## Verifying a change end to end

A unit-test pass is necessary, not sufficient, for anything touching ingestion or the page. Against
a live BirdNET-Go, check all of: the container log shows both streams connected and no reconnect
loop; `/health` reports `streams` true and `episode_accounting_ok` true; `/api/blobs` grows with a
`cameras` list per blob; the page draws them. If `estimate_dropped` climbs, a source name in
`cameras.yaml` does not match what BirdNET-Go reports (see `SETUP.md`, step 3).
