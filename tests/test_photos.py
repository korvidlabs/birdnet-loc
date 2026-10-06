"""Species photo cache and thumbnail geometry.

No pytest-asyncio in this project: the async surfaces are driven with
`asyncio.run`, which is enough because the worker is a plain consumer loop.
Nothing here touches the network -- every test injects a fake fetcher.
"""

import asyncio
import io
import json

import pytest
from PIL import Image

from birdloc.estimate import thumb_anchor, thumb_radius
from birdloc.photos import PhotoCache, fit_square, slug

SCI = "Cyanocitta cristata"
COMMON = "Blue Jay"


def jpeg_bytes(size=(320, 240), color=(30, 90, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


class FakeFetcher:
    """url -> bytes (or None). Records every URL it was asked for, in order."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[str] = []

    async def __call__(self, url, headers):
        self.calls.append(url)
        assert "User-Agent" in headers
        for prefix, body in self.responses.items():
            if url.startswith(prefix):
                return body
        return None


def cache(tmp_path, fetcher, **kw):
    return PhotoCache(
        str(tmp_path / "photos"),
        fetcher,
        birdnet_url="http://birdnet-go:8080",
        sleep=_no_sleep,
        **kw,
    )


async def _no_sleep(_seconds):
    return None


# -- image treatment -------------------------------------------------------


def test_a_320x240_frame_becomes_a_192_square_centre_crop():
    png = fit_square(jpeg_bytes((320, 240)))
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG"
    assert img.size == (192, 192)


def test_the_crop_is_centred_not_letterboxed():
    """ImageOps.fit keeps the middle of the frame: a wide image whose centre
    stripe is red must come back all red, with no background bars."""
    src = Image.new("RGB", (320, 240), (0, 0, 0))
    for x in range(40, 280):            # the centre 240 px, which fit() keeps
        for y in range(240):
            src.putpixel((x, y), (255, 0, 0))
    buf = io.BytesIO()
    src.save(buf, format="JPEG", quality=95)
    out = Image.open(io.BytesIO(fit_square(buf.getvalue())))
    for xy in ((2, 2), (96, 96), (189, 189)):
        r, g, b = out.getpixel(xy)
        assert r > 200 and g < 60 and b < 60, xy


def test_slug_keeps_only_filename_safe_characters():
    assert slug(SCI) == "Cyanocitta_cristata"
    assert slug("Bewick's Wren") == "Bewick_s_Wren"


# -- source order ----------------------------------------------------------


def test_birdnet_go_is_tried_first_and_wikipedia_is_never_touched(tmp_path):
    f = FakeFetcher({"http://birdnet-go:8080/api/v2/media/image/": jpeg_bytes()})
    pc = cache(tmp_path, f)
    assert asyncio.run(pc.fetch_one(SCI)) is True
    assert len(f.calls) == 1
    assert "wikipedia.org" not in " ".join(f.calls)
    assert pc.path_for(SCI).exists()


def test_wikipedia_is_the_fallback_scientific_name_first(tmp_path):
    summary = json.dumps({"originalimage": {"source": "https://upload/jay.jpg"}}).encode()
    f = FakeFetcher({
        "https://en.wikipedia.org/api/rest_v1/page/summary/Cyanocitta_cristata": summary,
        "https://upload/jay.jpg": jpeg_bytes(),
    })
    pc = cache(tmp_path, f)
    pc.enqueue(SCI, COMMON)
    assert asyncio.run(pc.fetch_one(SCI)) is True
    assert f.calls[0].startswith("http://birdnet-go:8080/")            # tried first
    assert f.calls[1].endswith("/summary/Cyanocitta_cristata")         # sci before common
    assert f.calls[2] == "https://upload/jay.jpg"
    assert pc.has(SCI)


def test_the_common_name_is_tried_after_the_scientific_one(tmp_path):
    summary = json.dumps({"thumbnail": {"source": "https://upload/bj.jpg"}}).encode()
    f = FakeFetcher({
        "https://en.wikipedia.org/api/rest_v1/page/summary/Blue_Jay": summary,
        "https://upload/bj.jpg": jpeg_bytes(),
    })
    pc = cache(tmp_path, f)
    pc.enqueue(SCI, COMMON)
    assert asyncio.run(pc.fetch_one(SCI)) is True
    assert any(c.endswith("/summary/Cyanocitta_cristata") for c in f.calls)
    assert any(c.endswith("/summary/Blue_Jay") for c in f.calls)


def test_thumbnail_source_is_used_when_originalimage_is_absent(tmp_path):
    summary = json.dumps({"thumbnail": {"source": "https://upload/small.jpg"}}).encode()
    f = FakeFetcher({
        "https://en.wikipedia.org/api/rest_v1/page/summary/": summary,
        "https://upload/small.jpg": jpeg_bytes((150, 150)),
    })
    pc = cache(tmp_path, f)
    assert asyncio.run(pc.fetch_one(SCI)) is True


# -- caching ---------------------------------------------------------------


def test_a_cached_species_is_served_from_memory_then_disk(tmp_path):
    f = FakeFetcher({"http://birdnet-go:8080/": jpeg_bytes()})
    pc = cache(tmp_path, f)
    asyncio.run(pc.fetch_one(SCI))

    # memory hit: no disk read needed
    assert pc.get(SCI) is not None

    # a fresh cache over the same directory finds the file without fetching
    empty = FakeFetcher({})
    pc2 = cache(tmp_path, empty)
    assert pc2.has(SCI) is True
    assert empty.calls == []
    assert pc2.enqueue(SCI) is False          # nothing to do


def test_a_failure_is_negatively_cached_and_retried_after_the_window(tmp_path):
    clock = {"t": 1000.0}
    f = FakeFetcher({})                        # every source misses
    pc = cache(tmp_path, f, now=lambda: clock["t"], retry_seconds=86400.0)

    assert pc.enqueue(SCI) is True
    asyncio.run(pc.drain())
    assert pc.has(SCI) is False
    assert pc.counters["failed"] == 1

    clock["t"] += 3600.0                       # an hour later: still suppressed
    assert pc.enqueue(SCI) is False

    clock["t"] += 86400.0                      # past the retry window
    assert pc.enqueue(SCI) is True


def test_an_undecodable_body_is_a_miss_not_a_crash(tmp_path):
    f = FakeFetcher({"http://birdnet-go:8080/": b"this is not an image"})
    pc = cache(tmp_path, f)
    assert asyncio.run(pc.fetch_one(SCI)) is False
    assert pc.has(SCI) is False


def test_enqueue_is_idempotent_while_the_species_is_in_flight(tmp_path):
    pc = cache(tmp_path, FakeFetcher({"http://birdnet-go:8080/": jpeg_bytes()}))
    assert pc.enqueue(SCI, COMMON) is True
    assert pc.enqueue(SCI, COMMON) is False
    assert pc.queue.qsize() == 1


def test_status_map_and_counters_report_by_scientific_name(tmp_path):
    f = FakeFetcher({"http://birdnet-go:8080/api/v2/media/image/Cyanocitta": jpeg_bytes()})
    pc = cache(tmp_path, f)
    pc.enqueue(SCI, COMMON)
    pc.enqueue("Nothing atall", "Nothing")
    asyncio.run(pc.drain())
    status = pc.status_map()
    assert status[SCI] is True
    assert status["Nothing atall"] is False
    assert pc.counters == {"cached": 1, "pending": 0, "failed": 1, "fetched": 1}


def test_the_worker_consumes_the_queue_and_survives_a_bad_species(tmp_path):
    f = FakeFetcher({"http://birdnet-go:8080/api/v2/media/image/Cyanocitta": jpeg_bytes()})
    pc = cache(tmp_path, f)
    pc.enqueue("Nothing atall")
    pc.enqueue(SCI)

    async def run():
        task = asyncio.create_task(pc.worker())
        await pc.queue.join()
        task.cancel()
    asyncio.run(run())

    assert pc.has(SCI) is True
    assert pc.has("Nothing atall") is False


def test_enqueue_never_fetches_in_the_caller(tmp_path):
    """The request path only touches the queue: no fetcher call happens until
    the worker runs."""
    f = FakeFetcher({"http://birdnet-go:8080/": jpeg_bytes()})
    pc = cache(tmp_path, f)
    pc.enqueue(SCI)
    assert f.calls == []
    assert pc.get(SCI) is None
    asyncio.run(pc.drain())
    assert f.calls != []
    assert pc.get(SCI) is not None


# -- thumb_anchor ----------------------------------------------------------


def test_thumb_radius_clamps_at_both_ends():
    assert thumb_radius(10.0) == 14.0            # 3.5 -> floor
    assert thumb_radius(1000.0) == 28.0          # 350 -> ceiling
    assert thumb_radius(60.0) == pytest.approx(21.0)


def test_a_blob_well_inside_the_image_is_not_moved():
    tx, ty, moved = thumb_anchor(500.0, 400.0, 60.0, (1071, 1008))
    assert (tx, ty, moved) == (500.0, 400.0, False)


def test_a_blob_past_the_east_edge_pulls_its_thumb_inside():
    tr = thumb_radius(40.0)                      # 14
    tx, ty, moved = thumb_anchor(1200.0, 500.0, 40.0, (1071, 1008))
    assert moved is True
    assert tx == pytest.approx(1071 - (tr + 4))
    assert ty == 500.0                           # only the offending axis moves


def test_a_blob_off_the_north_west_corner_lands_at_the_inset_corner():
    tr = thumb_radius(80.0)                      # 28
    tx, ty, moved = thumb_anchor(-50.0, -10.0, 80.0, (1071, 1008))
    assert moved is True
    assert (tx, ty) == (pytest.approx(tr + 4), pytest.approx(tr + 4))


def test_a_thumb_exactly_on_the_pad_boundary_is_left_alone():
    tr = thumb_radius(40.0)
    tx, _ty, moved = thumb_anchor(tr + 4, 500.0, 40.0, (1071, 1008))
    assert moved is False and tx == pytest.approx(tr + 4)


def test_an_explicit_thumb_r_overrides_the_clamp_rule():
    tx, _ty, moved = thumb_anchor(10.0, 500.0, 40.0, (1071, 1008), 40.0)
    assert moved is True and tx == pytest.approx(44.0)


def test_an_image_too_small_for_the_thumb_centres_it():
    tx, ty, moved = thumb_anchor(5.0, 5.0, 40.0, (20, 20))
    assert (tx, ty) == (10.0, 10.0) and moved is True


def test_the_blob_itself_is_never_moved_by_the_anchor():
    """thumb_anchor is pure geometry over its arguments; it returns a thumb
    position and nothing else, so an off-map estimate stays off the map."""
    args = (1200.0, 500.0, 40.0, (1071, 1008))
    first = thumb_anchor(*args)
    assert thumb_anchor(*args) == first
    assert args[0] == 1200.0
