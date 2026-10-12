"""A real `pending` snapshot list, decoded through Ingestor._handle_pending.

Shape as probed from BirdNET-Go's SSE stream (2026-09): a JSON
list, one element per (species, source), re-emitted about once a second.
"""

from datetime import datetime

import pytest

from birdnet_loc.bands import load_bands
from birdnet_loc.config import Config
from birdnet_loc.ingest import Ingestor

CFG = Config(window_seconds=4.0, close_after_seconds=5.0, max_episode_seconds=30.0)


def item(species, sci, source, source_id, first, hits, **kw):
    """One `pending` element, verbatim in shape."""
    d = {
        "species": species,
        "scientificName": sci,
        "status": "active",
        "firstDetected": first,
        "audioCapturedAt": first,
        "lastUpdated": first + 1,
        "source": source,
        "sourceID": source_id,
        "bestModelID": "BirdNET_V2.4",
        "hitCount": hits,
    }
    d.update(kw)
    return d


# The live 2026-09-08 House Finch: three cameras, firstDetected one second apart
FINCH = [
    item("House Finch", "Haemorhous mexicanus", "Side Yard South", "rtsp_7206157c",
         1788890108, 1),
    item("House Finch", "Haemorhous mexicanus", "Back Yard South", "rtsp_ea53cb00",
         1788890109, 1),
    item("House Finch", "Haemorhous mexicanus", "Doorbell", "rtsp_1f4b9d21",
         1788890113, 2),
]


def detection(species, sci, source, source_id, begin, end, conf=0.9):
    """One confirmed `detection` event (recon-birdnet-go-outputs §2c).

    The confirmation gate's only input: no episode is emitted without one at or
    above CONFIRM_MIN_CONF, so every decode test that expects an Event feeds one.
    """
    iso = lambda x: datetime.fromtimestamp(x).isoformat()
    return {
        "commonName": species,
        "scientificName": sci,
        "confidence": conf,
        "source": {"id": source_id, "displayName": source},
        "beginTime": iso(begin),
        "endTime": iso(end),
        "timestamp": iso(end + 8),
    }


# The detection BirdNET-Go confirms the finch with: one camera, as always.
FINCH_CONFIRMED = detection("House Finch", "Haemorhous mexicanus", "Side Yard South",
                            "rtsp_7206157c", 1788890108, 1788890120)


def ingestor(out):
    return Ingestor(CFG, load_bands(), out.append)


def test_a_real_pending_snapshot_becomes_one_multi_camera_episode():
    out = []
    ing = ingestor(out)
    ing._handle_pending(FINCH, now=1788890113.0)
    ing._handle_detection(FINCH_CONFIRMED)
    ing.assembler.tick(now=1788890125.0)

    assert len(out) == 1
    ev = out[0]
    assert ev.species == "House Finch"
    assert ev.sci_name == "Haemorhous mexicanus"
    assert ev.t == 1788890108.0
    assert {h.camera: h.hits for h in ev.hits} == {
        "Side Yard South": 1, "Back Yard South": 1, "Doorbell": 2,
    }
    assert all(h.band_db is None for h in ev.hits)
    # the confirmed confidence lands on the confirming camera and nowhere else
    assert {h.camera: h.conf for h in ev.hits} == {
        "Side Yard South": 0.9, "Back Yard South": None, "Doorbell": None,
    }
    assert ing.counts["pending"] == 3
    assert ing.stats["multi_camera_emitted"] == 1
    assert ing.stats["episodes_closed"] == (
        ing.stats["episodes_emitted"] + ing.stats["unconfirmed_dropped"]
    )


def test_an_unconfirmed_pending_snapshot_never_becomes_a_blob():
    """The owner's rule, end to end: pending alone reaches nothing."""
    out = []
    ing = ingestor(out)
    ing._handle_pending(FINCH, now=1788890113.0)
    ing.assembler.tick(now=1788890125.0)
    ing.assembler.tick(now=1788890125.0 + CFG.confirm_grace_seconds)
    assert out == []
    assert ing.stats["unconfirmed_dropped"] == 1
    assert ing.stats["episodes_emitted"] == 0


def test_a_sub_threshold_detection_event_does_not_confirm():
    out = []
    ing = ingestor(out)
    ing._handle_pending(FINCH, now=1788890113.0)
    ing._handle_detection(dict(FINCH_CONFIRMED, confidence=0.21))
    ing.assembler.tick(now=1788890125.0)
    ing.assembler.tick(now=1788890125.0 + CFG.confirm_grace_seconds)
    assert out == []
    assert ing.stats["confirmations_below_threshold"] == 1


def test_repeated_snapshots_keep_the_max_hitcount():
    out = []
    ing = ingestor(out)
    ing._handle_pending(FINCH, now=1788890113.0)
    grown = [dict(e, hitCount=e["hitCount"] + 3) for e in FINCH]
    ing._handle_pending(grown, now=1788890114.0)
    shrunk = [dict(e, hitCount=1) for e in FINCH]
    ing._handle_pending(shrunk, now=1788890115.0)
    ing._handle_detection(FINCH_CONFIRMED)
    ing.assembler.tick(now=1788890125.0)

    assert len(out) == 1
    assert {h.camera: h.hits for h in out[0].hits} == {
        "Side Yard South": 4, "Back Yard South": 4, "Doorbell": 5,
    }


def test_the_pairing_of_sourceid_and_source_is_learned():
    ing = ingestor([])
    ing._handle_pending(FINCH, now=1788890113.0)
    assert ing.sources.resolve("rtsp_7206157c") == "Side Yard South"
    assert ing.sources.resolve("rtsp_ea53cb00") == "Back Yard South"


def test_an_entry_with_only_a_sourceid_resolves_through_the_learned_map():
    out = []
    ing = ingestor(out)
    ing._handle_pending(FINCH, now=1788890113.0)
    hashed = dict(FINCH[0])
    hashed.pop("source")
    hashed["firstDetected"] = 1788890200
    ing._handle_detection(FINCH_CONFIRMED)
    ing._handle_pending([hashed], now=1788890200.0)
    ing._handle_detection(detection("House Finch", "Haemorhous mexicanus",
                                    "Side Yard South", "rtsp_7206157c",
                                    1788890200, 1788890212))
    ing.assembler.tick(now=1788890230.0)

    assert [h.camera for e in out for h in e.hits].count("Side Yard South") == 2


@pytest.mark.parametrize("bad", [
    {"species": "House Finch", "source": "Doorbell", "firstDetected": "not-a-number"},
    {"species": "House Finch", "source": "Doorbell", "firstDetected": 1788890108,
     "hitCount": "many"},
    {"species": "House Finch", "source": ["Doorbell"], "firstDetected": None},
    "not a dict",
    None,
])
def test_one_malformed_entry_never_costs_the_rest_of_the_snapshot(bad):
    """The other cameras in the list are exactly the multi-camera evidence the
    tool exists to capture; one bad element must not discard them."""
    out = []
    ing = ingestor(out)
    ing._handle_pending([bad] + FINCH, now=1788890113.0)
    ing._handle_detection(FINCH_CONFIRMED)
    ing.assembler.tick(now=1788890125.0)

    assert len(out) == 1
    assert len(out[0].hits) == 3


def test_entries_without_a_camera_or_a_time_are_skipped_quietly():
    out = []
    ing = ingestor(out)
    ing._handle_pending(
        [
            {"species": "House Finch", "firstDetected": 1788890108},        # no source
            {"species": "House Finch", "source": "Doorbell"},               # no time
            {"source": "Doorbell", "firstDetected": 1788890108},            # no species
        ],
        now=1788890113.0,
    )
    ing.assembler.tick(force=True, now=1788890113.0)
    assert out == []
    assert ing.counts["pending_bad_items"] == 0


def test_an_empty_or_missing_snapshot_is_harmless():
    ing = ingestor([])
    ing._handle_pending([])
    ing._handle_pending(None)
    assert ing.counts["pending"] == 0


def test_band_levels_reach_the_episode_through_the_soundlevel_stream():
    out = []
    ing = ingestor(out)
    ing._handle_pending(FINCH, now=1788890113.0)      # teaches the hashes
    ing.levels.update({
        "source": "rtsp_7206157c",
        "octave_bands": {
            "2.5_kHz": {"max_db": -61.0},
            "3.1_kHz": {"max_db": -52.5},
            "4.0_kHz": {"max_db": -70.0},
        },
    })
    ing._handle_pending(FINCH, now=1788890114.0)
    ing._handle_detection(FINCH_CONFIRMED)
    ing.assembler.tick(now=1788890125.0)

    by_cam = {h.camera: h for h in out[0].hits}
    assert by_cam["Side Yard South"].band_db == -52.5    # max over the finch band
    assert by_cam["Doorbell"].band_db is None
