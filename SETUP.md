# SETUP.md

A procedure for a coding agent to install and verify birdnet-loc against an existing BirdNET-Go. Execute
it top to bottom. Each step says what to run, what a good result looks like, and what to do when it
is not. Do not skip verification steps: a service that starts is not a service that works.

Assumptions: Docker with Compose v2 on the host, a BirdNET-Go instance reachable from that host, and
a shell on the host. Replace `BIRDNET` below with the BirdNET-Go base URL, e.g. `http://192.168.1.50:8080`.

## 1. Confirm BirdNET-Go is reachable and streams

```
curl -s -m 5 BIRDNET/api/v2/detections?limit=1 | head -c 400; echo
curl -s -m 15 -N BIRDNET/api/v2/soundlevels/stream | head -c 600; echo
```

Good: the first returns JSON. The second prints `event:`/`data:` lines within 15 s that include a
`source` name and per-band levels.

If the first fails: wrong URL or port; stop and fix it before continuing. If the second prints
nothing: sound-level reporting is off in BirdNET-Go (`realtime.audio.soundlevel.enabled`, or in the
web UI under audio settings). birdnet-loc cannot range without it. Turn it on and retry.

## 2. Collect the exact source names

```
curl -s -m 20 -N BIRDNET/api/v2/soundlevels/stream | grep -o '"source":"[^"]*"' | sort -u
```

Good: one line per audio source, e.g. `"source":"Back Yard North"`. Record them verbatim; they are
the keys in `cameras.yaml`. A name that differs by a character is a different camera to birdnet-loc.

If the names are RTSP URLs with credentials rather than display names, BirdNET-Go's display names
are not configured; set them in its RTSP source settings so credentials are not used as keys.

## 3. Clone and build the map directory

```
git clone https://github.com/korvidlabs/birdnet-loc.git && cd birdnet-loc
mkdir -p map && cp example/map/cameras.yaml map/
```

Put a north-up PNG of the area at `map/map.png`. Any top-down image works: an aerial, a plan, a
sketch. Then edit `map/cameras.yaml`:

- `image_size`: the PNG's pixel size (`python3 -c "from PIL import Image; print(Image.open('map/map.png').size)"`
  or any image tool).
- `scale_m_per_px`: metres per pixel. Measure one known distance on the image (a fence, a driveway,
  a building side) and divide metres by pixels.
- `cameras`: one entry per source name from step 2, with `px` (pixel position on the image),
  `bearing` (compass degrees the microphone faces, 0 = north, 90 = east) and `fov_deg` (180 is right
  for a camera microphone).

Validate the file without starting anything:

```
uv run --with pyyaml python3 -c "
import yaml; d = yaml.safe_load(open('map/cameras.yaml'))
assert d['scale_m_per_px'] > 0 and len(d['cameras']) >= 2
w, h = d['image_size']
for c in d['cameras']:
    x, y = c['px']; assert 0 <= x <= w and 0 <= y <= h, c['name']
print('ok:', [c['name'] for c in d['cameras']])"
```

If `uv` is not installed: `curl -LsSf https://astral.sh/uv/install.sh | sh`, or run the same check
with the system Python after `pip install pyyaml`.

## 4. Configure

```
cp .env.example .env
```

Edit `.env`: `BIRDNET_URL=BIRDNET`, `MAP_DIR=./map`, `DATA_DIR=./birdnet-loc-data`, and `TZ` to the
host's zone. Then read BirdNET-Go's confidence threshold and match it:

```
grep -A1 -i "threshold" <path to BirdNET-Go config.yaml> | head
```

If it is not `0.7`, add `CONFIRM_MIN_CONF=<value>` to `.env`. birdnet-loc only emits episodes BirdNET-Go
confirmed at or above this; a mismatch either drops real birds or passes phantom ones.

`mkdir -p birdnet-loc-data` so the bind mount is owned by the invoking user. The container runs as
uid 1000 by default; if `id -u` is different, add `BIRDLOC_UID=<uid>` and `BIRDLOC_GID=<gid>` to `.env`.

## 5. Start

```
docker compose up -d --build
docker compose logs -f birdnet-loc
```

Good, within about 20 s: lines reporting the soundlevels and detections streams connected, and no
repeating `stream error` lines. Ctrl-C the log tail.

If `stream error: All connection attempts failed` repeats: the container cannot reach `BIRDNET_URL`.
`localhost` inside the container is the container; use the host's LAN address or, when BirdNET-Go
is also in Compose on the same network, its service name.

## 6. Verify all four surfaces

```
curl -s localhost:9522/health | python3 -m json.tool
```

Good: `"ok": true`, `"streams"` with both entries `true`, `"cameras"` equal to the number of entries
in `cameras.yaml`, `"episode_accounting_ok": true`. Early on `"blobs"` may be 0; that is fine.

```
curl -s localhost:9522/api/cameras | python3 -m json.tool | head -30
```

Good: every source name you recorded in step 2, with its pixel position.

Wait for birds. In an active yard the first estimate arrives within minutes of a confirmed detection;
at night it may take until dawn. Then:

```
curl -s localhost:9522/api/blobs | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['count'], 'blobs'); print(d['blobs'][-1] if d['blobs'] else 'none yet')"
```

Good: a count above 0, and the last row carries `species`, `x_px`/`y_px`/`radius_px`, a `cameras`
list naming the sources that heard it, and `confirmed_conf` at or above the threshold.

Finally open `http://<host>:9522/` in a browser: the map with estimates, the species list at the right,
and clicking one shows its cameras.

## 7. Read the health counters after an hour

```
curl -s localhost:9522/health | python3 -c "import sys,json; h=json.load(sys.stdin); print({k:h[k] for k in ('blobs','blobs_stored','store_rejected','estimate_dropped','sources_known')}); print(h['counts']); print(h['calib'])"
```

- `estimate_dropped` above 0 means episodes arrived whose sources are not in `cameras.yaml`. The
  log line `episode dropped, no known camera` names them; fix the spelling in `cameras.yaml` and
  `docker compose restart birdnet-loc`.
- `counts` shows `unconfirmed_dropped`. A large share of closed episodes dropping unconfirmed usually
  means `CONFIRM_MIN_CONF` is above BirdNET-Go's threshold; recheck step 4.
- `calib.species_calibrated` grows as species collect `min_n` samples. Until a species is calibrated,
  its estimates use `FALLBACK_RANGE_M`; positions tighten over the first days.

## 8. Tuning, when the geometry asks for it

Defaults in `docker-compose.yml` suit a lot about 35 m across. For a larger property raise
`MAX_RANGE_M` and `MAX_RADIUS_M`; for microphones far apart raise `WINDOW_SECONDS` a little (sound
takes ~3 ms per metre, so this is rarely the issue; clock skew between sources is). Every knob is
documented where it is set. Change one, restart, and give it a day before judging.

## Done when

After a day of detections, list the species the instance has heard that have no song band yet and add
the ones that matter. `docs/bands.md` explains how to choose a range; unknown species use `default`:

```sh
uv run bands-tool missing --birdnet-url BIRDNET --min-count 20
uv run bands-tool add "<common name>" <lo_hz> <hi_hz>
```

Steps 6 and 7 read as "Good" from the host's own shell, and the page draws estimates over your map. Report
those readings, not the fact that the container started.
