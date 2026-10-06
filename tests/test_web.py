"""The HTTP surface, through Starlette's TestClient.

The client is used WITHOUT its context manager on purpose: entering it runs the
lifespan, which starts the SSE ingest task against a BirdNET-Go that is not
there. These tests are about the routes, not the streams.
"""

import time

import pytest
from fastapi.testclient import TestClient

from birdloc import web
from birdloc.estimate import Blob


@pytest.fixture()
def client():
    web.state.store._ring.clear()
    return TestClient(web.app)


def blob(t, species="Blue Jay", n_cams=1):
    return Blob(
        species=species,
        sci_name="Cyanocitta cristata",
        t=t,
        x_px=500.0,
        y_px=400.0,
        radius_px=120.0,
        confidence=0.5,
        cameras=[{"name": f"C{i}", "hits": 3, "range_m": 6.0, "ref_db": None,
                  "conf": 0.83 if i == 0 else None}
                 for i in range(n_cams)],
        confirmed_conf=0.83,
        confirmed_by="C0",
    )


# -- /health ---------------------------------------------------------------


def test_health_is_ok_and_reports_the_counters(client):
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["cameras"] == 7
    for key in ("blobs", "streams", "counts", "episodes_emitted", "blobs_stored",
                "store_rejected", "estimate_dropped", "unknown_sources",
                "multi_camera_emitted", "calib", "episodes_closed",
                "episodes_awaiting", "unconfirmed_dropped", "confirm_min_conf",
                "confirm_grace_seconds", "episode_accounting_ok"):
        assert key in body, key
    assert "species_calibrated" in body["calib"]
    assert isinstance(body["unknown_sources"], list)


def test_health_accounts_for_every_emitted_episode(client):
    """episodes_emitted == stored + rejected + dropped, with every term non-zero:
    one storable episode, one duplicate (t, species), one all-unknown-camera."""
    from birdloc.estimate import Event, Hit
    t = time.time()
    good = Event(species="Osprey", sci_name="Pandion haliaetus", t=t, hits=(Hit("Doorbell", 2),))
    web.state.ingestor.assembler.emitted += 3
    web.state.handle_event(good)
    web.state.handle_event(good)                                   # rejected
    web.state.handle_event(Event(species="Osprey", sci_name="Pandion haliaetus",
                                 t=t + 1, hits=(Hit("Nowhere", 2),)))  # dropped
    body = client.get("/health").json()
    assert body["blobs_stored"] >= 1 and body["store_rejected"] >= 1 and body["estimate_dropped"] >= 1
    assert body["episodes_emitted"] == (
        body["blobs_stored"] + body["store_rejected"] + body["estimate_dropped"]
    )


def test_health_accounts_for_every_closed_episode(client):
    """The gate's invariant, every term non-zero:
    episodes_closed == episodes_emitted + unconfirmed_dropped (+ awaiting)."""
    from birdloc.ingest import EpisodeAssembler
    a = EpisodeAssembler(web.state.cfg, lambda ev: None)
    # one confirmed episode, one that is never confirmed, one still in grace
    a.observe("House Finch", "Haemorhous mexicanus", "Doorbell", 2, 1000.0, now=1000.0)
    a.confirm("House Finch", "Doorbell", 0.91, 1000.0, 1012.0)
    a.observe("Wild Turkey", "Meleagris gallopavo", "Doorbell", 1, 1000.0, now=1000.0)
    a.tick(now=1010.0)                                    # both close
    a.tick(now=1010.0 + web.state.cfg.confirm_grace_seconds)   # the turkey expires
    a.observe("Sora", "Porzana carolina", "Doorbell", 1, 2000.0, now=2000.0)
    a.tick(now=2010.0)                                    # closes, still in grace

    assert a.emitted == 1 and a.unconfirmed_dropped == 1 and a.awaiting_count == 1
    assert a.closed == a.emitted + a.unconfirmed_dropped + a.awaiting_count

    saved = web.state.ingestor.assembler
    web.state.ingestor.assembler = a
    try:
        body = client.get("/health").json()
    finally:
        web.state.ingestor.assembler = saved
    assert body["episodes_closed"] == 3
    assert body["episodes_emitted"] == 1
    assert body["unconfirmed_dropped"] == 1
    assert body["episodes_awaiting"] == 1
    assert body["episode_accounting_ok"] is True
    # The shipped floor: docker-compose.yml sets CONFIRM_MIN_CONF=0.75, and
    # conftest pins the suite to the same number. This was 0.7 until the owner
    # raised it on 2026-09-09; /health must report what is actually in force.
    assert body["confirm_min_conf"] == 0.75


def test_a_rejected_store_is_counted_not_swallowed(client):
    from birdloc.estimate import Event, Hit

    before = client.get("/health").json()
    ev = Event(species="Blue Jay", sci_name="Cyanocitta cristata", t=time.time(),
               hits=(Hit("Doorbell", 3),))
    web.state.handle_event(ev)
    web.state.handle_event(ev)          # same (t, species): the store refuses it
    after = client.get("/health").json()

    assert after["blobs_stored"] == before["blobs_stored"] + 1
    assert after["store_rejected"] == before["store_rejected"] + 1


def test_an_episode_with_no_known_camera_is_counted_as_dropped(client):
    from birdloc.estimate import Event, Hit

    before = client.get("/health").json()
    web.state.handle_event(Event(species="Blue Jay", sci_name="", t=time.time(),
                                 hits=(Hit("Nowhere At All", 3),)))
    after = client.get("/health").json()

    assert after["estimate_dropped"] == before["estimate_dropped"] + 1
    assert "Nowhere At All" in after["unknown_sources"]


# -- /api/blobs ------------------------------------------------------------


def test_blobs_since_filters_by_time(client):
    now = time.time()
    web.state.store.add(blob(now - 600, species="Old One"))
    web.state.store.add(blob(now - 10, species="Recent One"))

    everything = client.get("/api/blobs?since=0").json()
    assert everything["count"] == 2
    assert everything["since"] == 0.0

    recent = client.get(f"/api/blobs?since={now - 60}").json()
    assert [b["species"] for b in recent["blobs"]] == ["Recent One"]


def test_blob_rows_carry_the_documented_fields(client):
    web.state.store.add(blob(time.time(), n_cams=2))
    row = client.get("/api/blobs?since=0").json()["blobs"][0]
    for key in ("species", "t", "x_px", "y_px", "radius_px", "confidence",
                "cameras", "n_cameras", "color", "clamped", "degraded",
                "confirmed_conf", "confirmed_by"):
        assert key in row, key
    assert row["confirmed_conf"] == 0.83 and row["confirmed_by"] == "C0"
    assert row["n_cameras"] == 2
    assert row["cameras"][0]["range_m"] == 6.0
    assert row["color"].startswith("#")


def test_blobs_can_be_filtered_by_species(client):
    now = time.time()
    web.state.store.add(blob(now, species="Blue Jay"))
    web.state.store.add(blob(now, species="House Finch"))
    body = client.get("/api/blobs?since=0&species=House Finch").json()
    assert [b["species"] for b in body["blobs"]] == ["House Finch"]


def test_blobs_with_no_rows_is_an_empty_list_not_an_error(client):
    body = client.get("/api/blobs?since=0").json()
    assert body["count"] == 0 and body["blobs"] == []


# -- /api/cameras ----------------------------------------------------------


def test_cameras_reports_the_real_placements(client):
    body = client.get("/api/cameras").json()
    assert body["image_size"] == list(web.state.cameras.image_size)
    assert body["scale_m_per_px"] == pytest.approx(web.state.cameras.scale_m_per_px)
    names = {c["name"] for c in body["cameras"]}
    assert {"Back Yard South", "Side Yard South", "Doorbell"} <= names
    assert len(body["cameras"]) == 7
    one = next(c for c in body["cameras"] if c["name"] == "Doorbell")
    assert one["bearing"] == 97 and one["fov_deg"] == 180


# -- the page --------------------------------------------------------------


def test_the_page_loads_and_carries_the_map_dimensions(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    w, h = web.state.cameras.image_size
    assert f"viewBox=\"0 0 {w} {h}\"" in res.text
    # it must fetch its own range, never the whole 24 h (finding J)
    assert "api/blobs?since=0" not in res.text
    assert "fetchBlobs" in res.text


def test_the_map_image_is_served(client):
    res = client.get("/map.png")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"


# -- species photos --------------------------------------------------------


def test_photo_is_404_until_the_worker_has_cached_it_then_200(client, tmp_path):
    """The request path never fetches: the endpoint 404s, the (test-driven)
    worker runs, and only then does the same URL serve a PNG."""
    import asyncio

    from birdloc import photos as photos_mod

    original = web.state.photos
    fetched = {"n": 0}

    async def fake_fetch(url, headers):
        fetched["n"] += 1
        import io

        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (320, 240), (10, 120, 200)).save(buf, format="JPEG")
        return buf.getvalue()

    web.state.photos = photos_mod.PhotoCache(
        str(tmp_path / "photos"), fake_fetch, birdnet_url="http://birdnet-go:8080"
    )
    try:
        assert client.get("/api/photo/Cyanocitta%20cristata").status_code == 404
        assert fetched["n"] == 0            # the request path fetched nothing

        asyncio.run(web.state.photos.drain())

        res = client.get("/api/photo/Cyanocitta%20cristata")
        assert res.status_code == 200
        assert res.headers["content-type"] == "image/png"
        assert res.headers["cache-control"] == "max-age=86400"
        assert res.content[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        web.state.photos = original


def test_photo_serves_a_pre_seeded_cache_file(client, tmp_path):
    import io

    from PIL import Image

    from birdloc import photos as photos_mod

    d = tmp_path / "photos"
    d.mkdir(parents=True)
    buf = io.BytesIO()
    Image.new("RGB", (192, 192), (200, 40, 40)).save(buf, format="PNG")
    (d / "Cyanocitta_cristata.png").write_bytes(buf.getvalue())

    original = web.state.photos
    web.state.photos = photos_mod.PhotoCache(str(d), _never_fetch)
    try:
        res = client.get("/api/photo/Cyanocitta%20cristata")
        assert res.status_code == 200 and res.headers["content-type"] == "image/png"
        assert client.get("/api/photo/Nothing%20atall").status_code == 404
    finally:
        web.state.photos = original


async def _never_fetch(url, headers):        # pragma: no cover - must not run
    raise AssertionError("the request path must never fetch")


def test_photos_status_map_and_health_counters(client, tmp_path):
    from birdloc import photos as photos_mod

    original = web.state.photos
    web.state.photos = photos_mod.PhotoCache(str(tmp_path / "photos"), _never_fetch)
    try:
        web.state.photos.enqueue("Cyanocitta cristata", "Blue Jay")
        body = client.get("/api/photos").json()
        assert body == {"Cyanocitta cristata": False}
        counters = client.get("/health").json()["photos"]
        assert counters["cached"] == 0 and counters["pending"] == 1
    finally:
        web.state.photos = original


def test_blob_rows_carry_the_thumbnail_geometry(client):
    web.state.store.add(blob(time.time()))
    row = client.get("/api/blobs?since=0").json()["blobs"][0]
    for key in ("thumb_x_px", "thumb_y_px", "thumb_r_px", "thumb_moved",
                "photo_url", "sci_name"):
        assert key in row, key
    # 500/400 with r=120 is well inside the 1071x1008 map: unmoved, r clamped to 28
    assert row["thumb_x_px"] == 500.0 and row["thumb_y_px"] == 400.0
    assert row["thumb_r_px"] == 28.0
    assert row["thumb_moved"] is False


def test_an_off_map_blob_gets_its_thumb_pulled_inside(client):
    b = blob(time.time())
    off = Blob(**{**b.__dict__, "x_px": 1300.0})
    web.state.store.add(off)
    row = client.get("/api/blobs?since=0").json()["blobs"][0]
    assert row["x_px"] == 1300.0              # the blob itself is NOT moved
    assert row["thumb_moved"] is True
    assert row["thumb_x_px"] == pytest.approx(web.state.cameras.image_size[0] - 32, abs=0.1)


def test_the_page_draws_thumbnails_above_the_blobs(client):
    text = client.get("/").text
    assert 'id="thumbs"' in text and 'id="thumbdefs"' in text
    assert "drawThumbs" in text and "clusterKey" in text
    # thumbs must not share the blobs' per-view alpha budget
    assert text.index('<g id="blobs">') < text.index('<g id="thumbs">')
