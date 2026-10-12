"""Band-key mapping, source identity, and episode assembly from the `pending` stream."""

from birdnet_loc.bands import band_keys, load_bands, band_level_db
from birdnet_loc.config import Config
from birdnet_loc.ingest import EpisodeAssembler, SourceMap

CFG = Config(window_seconds=4.0, close_after_seconds=5.0, max_episode_seconds=30.0)


def entry(species, camera, hits, first, sci="Cyanocitta cristata"):
    """One element of a `pending` snapshot (recon-birdnet-go-outputs §2a)."""
    return dict(species=species, sci_name=sci, camera=camera, hit_count=hits, first_detected=first)


def asm(out, level_fn=None, cfg=CFG):
    return EpisodeAssembler(cfg, out.append, level_fn)


def confirm(a, species="Blue Jay", camera="A", t=1000.0, conf=0.9, span=12.0):
    """The confirmation gate's input: a BirdNET-Go `detection` at/above threshold.

    Nothing is emitted without one (ingest.py, "the confirmation gate"), so every
    assembly test that expects an Event has to supply one. `span` is the real
    detection window length (beginTime..endTime is 12 s on this build)."""
    return a.confirm(species, camera, conf, t, t + span)


# -- bands -----------------------------------------------------------------


def test_chickadee_band_maps_to_the_expected_octave_keys():
    table = load_bands()
    assert table.band_for("Black-capped Chickadee") == (2500.0, 4000.0)
    assert table.keys_for("Black-capped Chickadee") == ["2.5_kHz", "3.1_kHz", "4.0_kHz"]


def test_unknown_species_uses_the_default_band():
    table = load_bands()
    assert table.band_for("Canyon Wren") == (2500.0, 6300.0)
    assert table.keys_for("Canyon Wren") == ["2.5_kHz", "3.1_kHz", "4.0_kHz", "5.0_kHz", "6.3_kHz"]


def test_low_band_species_map_to_hz_keys():
    table = load_bands()
    assert table.keys_for("Rock Pigeon") == ["250.0_Hz", "315.0_Hz", "400.0_Hz", "500.0_Hz"]
    assert table.keys_for("Mourning Dove") == ["400.0_Hz", "500.0_Hz", "630.0_Hz"]


def test_narrow_band_still_selects_the_nearest_key():
    assert band_keys(2600, 2700) == ["2.5_kHz"]


def test_band_level_takes_the_max_over_the_species_band():
    payload = {
        "2.5_kHz": {"max_db": -70.0},
        "3.1_kHz": {"max_db": -55.5},
        "4.0_kHz": {"max_db": -80.0},
        "8.0_kHz": {"max_db": -10.0},
    }
    assert band_level_db(payload, "Black-capped Chickadee", load_bands()) == -55.5
    assert band_level_db({}, "Black-capped Chickadee", load_bands()) is None


# -- source identity -------------------------------------------------------


def test_source_map_learns_hash_from_a_detection_source_object():
    sm = SourceMap()
    name = sm.learn_from_source_obj({"id": "rtsp_7206157c", "displayName": "Side Yard South"})
    assert name == "Side Yard South"
    assert sm.resolve("rtsp_7206157c") == "Side Yard South"


def test_source_map_resolves_a_url_id_via_audio_sources():
    sm = SourceMap()
    sm.learn_uri("rtsp://host:554/h264Preview_06_sub", "Side Yard South")
    assert sm.learn_from_source_obj({"id": "rtsp://host:554/h264Preview_06_sub"}) == "Side Yard South"


# -- episode assembly ------------------------------------------------------


def test_single_camera_episode_emits_one_hit():
    out = []
    a = asm(out)
    a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=1000.0)
    a.observe(**entry("Blue Jay", "A", 2, 1000.0), now=1001.0)
    assert confirm(a) is True
    a.tick(now=1010.0)

    assert len(out) == 1
    assert [(h.camera, h.hits) for h in out[0].hits] == [("A", 2)]
    assert out[0].t == 1000.0
    assert a.emitted == 1 and a.multi_camera == 0


def test_three_cameras_across_snapshots_become_one_event_with_max_hitcounts():
    out = []
    a = asm(out)
    # snapshot 1: one camera; 2: two; 3: three, with A's hitCount grown
    a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=1000.0)
    a.observe(**entry("Blue Jay", "A", 2, 1000.0), now=1001.0)
    a.observe(**entry("Blue Jay", "B", 1, 1001.0), now=1001.0)
    a.observe(**entry("Blue Jay", "A", 3, 1000.0), now=1002.0)
    a.observe(**entry("Blue Jay", "B", 2, 1001.0), now=1002.0)
    a.observe(**entry("Blue Jay", "C", 1, 1002.0), now=1002.0)
    # a late snapshot must never lower a hitCount already seen
    a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=1003.0)
    confirm(a, camera="B", t=1001.0)
    a.tick(now=1009.0)

    assert len(out) == 1
    assert {h.camera: h.hits for h in out[0].hits} == {"A": 3, "B": 2, "C": 1}
    # the confidence lands on the confirming camera only
    assert {h.camera: h.conf for h in out[0].hits} == {"A": None, "B": 0.9, "C": None}
    assert a.multi_camera == 1


def test_episode_stays_open_while_pending_repeats_and_closes_after_absence():
    out = []
    a = asm(out)
    a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=1000.0)
    for t in (1001.0, 1002.0, 1003.0, 1004.0, 1005.0, 1006.0):
        a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=t)
        a.tick(now=t)
    assert out == [] and a.open_count == 1
    confirm(a)

    a.tick(now=1006.0 + CFG.close_after_seconds - 0.5)
    assert out == []
    a.tick(now=1006.0 + CFG.close_after_seconds)
    assert len(out) == 1 and a.open_count == 0


def test_episode_is_capped_by_max_episode_seconds():
    """The cap must actually fire: a bird singing without a break for 40 s is
    closed at 30 s, not held open until it stops."""
    out = []
    a = asm(out)
    confirm_done = False
    for t in range(0, 40):
        a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=1000.0 + t)
        if not confirm_done:
            confirm(a)
            confirm_done = True
        a.tick(now=1000.0 + t)

    # exactly one close, and it happened at the cap -- never at the 5 s absence
    # rule, which cannot fire while a snapshot arrives every second
    assert len(out) == 1
    assert out[0].species == "Blue Jay" and out[0].t == 1000.0
    assert a.emitted == 1
    # and the snapshots after the cap did NOT silently reopen the same (t,
    # species) the store already holds (finding C)
    assert a.open_count == 0
    assert a.reopen_refused > 0


def test_a_closed_episode_is_not_reopened_by_a_late_snapshot():
    out = []
    a = asm(out)
    a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=1000.0)
    confirm(a)
    a.tick(force=True, now=1000.0)
    assert len(out) == 1 and a.open_count == 0

    a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=1006.0)
    a.tick(now=1020.0)
    assert len(out) == 1              # no second blob for the same (t, species)
    assert a.reopen_refused == 1


def test_a_closed_key_expires_and_can_be_reopened_much_later():
    out = []
    a = asm(out)
    a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=1000.0)
    confirm(a)
    a.tick(force=True, now=1000.0)
    # the same firstDetected long after the TTL is a fresh bird, not a re-open
    later = 1000.0 + a.closed_ttl + 1.0
    a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=later)
    confirm(a)
    a.tick(force=True, now=later)
    assert len(out) == 2


def test_two_species_in_the_same_second_are_two_events():
    out = []
    a = asm(out)
    a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=1000.0)
    a.observe(**entry("House Finch", "A", 1, 1000.0, sci="Haemorhous mexicanus"), now=1000.0)
    confirm(a, "Blue Jay")
    confirm(a, "House Finch")
    a.tick(force=True, now=1000.0)
    assert {e.species for e in out} == {"Blue Jay", "House Finch"}
    assert len(out) == 2


def test_firstdetected_outside_the_window_opens_a_second_episode():
    out = []
    a = asm(out)
    a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=1000.0)
    a.observe(**entry("Blue Jay", "A", 1, 1003.0), now=1003.0)   # within 4 s of t0
    a.observe(**entry("Blue Jay", "A", 1, 1030.0), now=1030.0)   # far outside
    confirm(a, t=1000.0)
    confirm(a, t=1030.0)
    a.tick(force=True, now=1030.0)
    assert sorted(e.t for e in out) == [1000.0, 1030.0]


def test_the_live_house_finch_case_is_one_three_camera_episode():
    """The real 2026-09-08 capture: one House Finch, three cameras,
    firstDetected 1788890108 / ...109 / ...113. Fixed
    `firstDetected // WINDOW` buckets split 108/109 (bucket 1788890108) from
    113 (bucket 1788890112) and produced two blobs where three cameras had
    agreed on one bird (finding D). Tolerance matching keeps them together."""
    out = []
    a = asm(out)
    a.observe(**entry("House Finch", "Side Yard South", 1, 1788890108.0,
                      sci="Haemorhous mexicanus"), now=1788890108.0)
    a.observe(**entry("House Finch", "Back Yard South", 1, 1788890109.0,
                      sci="Haemorhous mexicanus"), now=1788890109.0)
    a.observe(**entry("House Finch", "Doorbell", 2, 1788890113.0,
                      sci="Haemorhous mexicanus"), now=1788890113.0)
    confirm(a, "House Finch", "Doorbell", 1788890113.0)
    a.tick(now=1788890120.0)

    assert len(out) == 1
    ev = out[0]
    assert {h.camera for h in ev.hits} == {"Side Yard South", "Back Yard South", "Doorbell"}
    assert ev.t == 1788890108.0
    assert a.multi_camera == 1


def test_tolerance_matching_chains_only_within_a_window_of_the_latest():
    """113 attaches because it is within 4 s of 109, which is itself attached.
    Chaining is bounded by MAX_EPISODE_SECONDS from t0 (see the next test)."""
    out = []
    a = asm(out)
    a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=1000.0)
    a.observe(**entry("Blue Jay", "B", 1, 1003.0), now=1003.0)   # within 4 s of t0
    a.observe(**entry("Blue Jay", "C", 1, 1006.0), now=1006.0)   # within 4 s of 1003
    confirm(a, camera="C", t=1006.0)
    a.tick(force=True, now=1006.0)
    assert len(out) == 1
    assert {h.camera for h in out[0].hits} == {"A", "B", "C"}


def test_band_level_is_the_max_seen_over_the_episode_life():
    out = []
    levels = {"A": iter([-70.0, -55.0, -60.0])}

    def level_fn(camera, species):
        return next(levels[camera], -60.0)

    a = asm(out, level_fn)
    for t in (1000.0, 1001.0, 1002.0):
        a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=t)
    confirm(a)
    a.tick(force=True, now=1002.0)
    assert out[0].hits[0].band_db == -55.0


def test_missing_band_data_stays_none():
    out = []
    a = asm(out)
    a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=1000.0)
    confirm(a)
    a.tick(force=True, now=1000.0)
    assert out[0].hits[0].band_db is None


def test_confirmed_confidence_attaches_to_an_open_episodes_camera():
    out = []
    a = asm(out)
    a.observe(**entry("Blue Jay", "A", 2, 1000.0), now=1000.0)
    assert a.attach_confidence("Blue Jay", "A", 0.84) is True
    assert a.attach_confidence("Blue Jay", "Z", 0.9) is False      # camera not in the episode
    assert a.attach_confidence("House Finch", "A", 0.9) is False   # no such episode
    a.tick(force=True, now=1000.0)
    assert out[0].hits[0].conf == 0.84


def test_confidence_goes_to_the_episode_whose_window_contains_the_detection():
    """Two episodes of one species open at once: the confirmation belongs to the
    one whose window holds the detection time, not simply the newest -- and it
    is the only one that may be emitted."""
    out = []
    a = asm(out)
    a.observe(**entry("Blue Jay", "A", 2, 1000.0), now=1000.0)
    a.observe(**entry("Blue Jay", "A", 2, 1100.0), now=1100.0)
    assert a.open_count == 2

    a.attach_confidence("Blue Jay", "A", 0.84, 1001.0)   # the older episode
    a.tick(force=True, now=1101.0)

    assert [e.t for e in out] == [1000.0]
    assert out[0].hits[0].conf == 0.84
    assert a.unconfirmed_dropped == 1                    # the 1100.0 episode


# -- the confirmation gate -------------------------------------------------


def test_an_unconfirmed_episode_is_never_emitted():
    """The owner's rule: nothing reaches the map that BirdNET-Go did not confirm
    at or above its threshold on one of the episode's own cameras."""
    out = []
    a = asm(out)
    a.observe(**entry("Wild Turkey", "A", 3, 1000.0), now=1000.0)
    a.tick(now=1010.0)
    assert out == [] and a.closed == 1 and a.awaiting_count == 1
    # still inside the grace: not dropped yet, and still not emitted
    a.tick(now=1010.0 + CFG.confirm_grace_seconds - 1.0)
    assert out == [] and a.unconfirmed_dropped == 0
    a.tick(now=1010.0 + CFG.confirm_grace_seconds)
    assert out == [] and a.unconfirmed_dropped == 1 and a.awaiting_count == 0
    assert a.closed == a.emitted + a.unconfirmed_dropped


def test_the_dropped_species_is_logged_so_the_impostor_list_is_visible(caplog):
    import logging
    out = []
    a = asm(out)
    a.observe(**entry("Sora", "A", 1, 1000.0), now=1000.0)
    a.observe(**entry("Sora", "B", 1, 1000.0), now=1000.0)
    a.tick(now=1010.0)                      # closes, unconfirmed -> awaiting
    with caplog.at_level(logging.INFO, logger="birdnet_loc.ingest"):
        a.tick(now=1010.0 + CFG.confirm_grace_seconds)
    msgs = [r.getMessage() for r in caplog.records if "never confirmed" in r.getMessage()]
    assert len(msgs) == 1
    assert "Sora" in msgs[0] and "A, B" in msgs[0]


def test_confirmation_after_close_but_inside_the_grace_still_emits():
    """Measured: the confirming detection lands p50 14 s after firstDetected,
    which can be after the episode's 5 s absence close."""
    out = []
    a = asm(out)
    a.observe(**entry("Blue Jay", "A", 2, 1000.0), now=1000.0)
    a.tick(now=1010.0)
    assert out == [] and a.awaiting_count == 1

    assert confirm(a, t=1000.0) is True
    assert len(out) == 1 and out[0].hits[0].conf == 0.9
    assert a.awaiting_count == 0 and a.unconfirmed_dropped == 0
    assert a.closed == a.emitted + a.unconfirmed_dropped


def test_confirmation_after_the_grace_has_expired_is_too_late():
    out = []
    a = asm(out)
    a.observe(**entry("Blue Jay", "A", 2, 1000.0), now=1000.0)
    a.tick(now=1010.0)
    a.tick(now=1010.0 + CFG.confirm_grace_seconds + 1.0)
    assert a.unconfirmed_dropped == 1

    assert confirm(a, t=1000.0) is False
    assert out == []


def test_a_sub_threshold_detection_is_not_a_confirmation():
    """The SSE detection stream is not threshold-filtered: 10 of 27 events in a
    300 s capture were below 0.7 (min 0.21). Those are the phantoms."""
    out = []
    a = asm(out)
    a.observe(**entry("Green-winged Teal", "A", 1, 1000.0), now=1000.0)
    assert a.confirm("Green-winged Teal", "A", 0.69, 1000.0, 1012.0) is False
    a.tick(now=1010.0)
    a.tick(now=1010.0 + CFG.confirm_grace_seconds)
    assert out == [] and a.unconfirmed_dropped == 1
    assert a.confirmations_seen == 1 and a.confirmations_below_threshold == 1


def test_a_confirmation_for_another_species_or_camera_does_not_confirm():
    out = []
    a = asm(out)
    a.observe(**entry("Virginia Rail", "A", 1, 1000.0), now=1000.0)
    assert confirm(a, "House Finch", "A", 1000.0) is False     # wrong species
    assert confirm(a, "Virginia Rail", "Z", 1000.0) is False    # camera not in it
    a.tick(now=1010.0)
    a.tick(now=1010.0 + CFG.confirm_grace_seconds)
    assert out == [] and a.unconfirmed_dropped == 1


def test_a_confirmation_outside_the_episode_window_does_not_confirm():
    """[beginTime, endTime] must overlap [t0 - W, last_first + W]."""
    out = []
    a = asm(out)
    a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=1000.0)
    # a detection that ends before t0 - W, and one that starts after +W
    assert a.confirm("Blue Jay", "A", 0.95, 900.0, 995.0) is False
    assert a.confirm("Blue Jay", "A", 0.95, 1005.0, 1017.0) is False
    a.tick(now=1010.0)
    a.tick(now=1010.0 + CFG.confirm_grace_seconds)
    assert out == [] and a.unconfirmed_dropped == 1


def test_a_confirmation_that_arrives_before_its_pending_entries_still_counts():
    """Order independence: the ring of recent confirmations is consulted when an
    episode closes. Not observed in the 300 s capture, but the margin was one
    tick, so the gate does not rely on stream order."""
    out = []
    a = asm(out)
    assert a.confirm("Blue Jay", "A", 0.88, 1000.0, 1012.0) is False   # nothing open yet
    a.observe(**entry("Blue Jay", "A", 2, 1001.0), now=1001.0)
    a.tick(now=1011.0)
    assert len(out) == 1 and out[0].hits[0].conf == 0.88


def test_the_ring_forgets_a_confirmation_older_than_its_ttl():
    out = []
    a = asm(out)
    a.confirm("Blue Jay", "A", 0.88, 1000.0, 1012.0, now=1000.0)
    later = 1000.0 + CFG.confirm_ring_seconds + 1.0
    a.observe(**entry("Blue Jay", "A", 2, 1000.0), now=later)
    a.tick(now=later + 10.0)
    assert out == []


def test_source_map_does_not_read_birdnet_db():
    """seed_from_db is gone: nothing in the tool opens BirdNET-Go's SQLite."""
    assert not hasattr(SourceMap(), "seed_from_db")


def test_a_chain_of_firstdetected_values_cannot_outlive_max_episode_seconds():
    """A drip of values 4 s apart must not merge forever (re-review defect 2)."""
    out = []
    a = asm(out)
    t = 1000.0
    for i in range(12):                       # 0, 4, 8, ... 44 s
        a.observe(**entry("Blue Jay", f"C{i}", 1, t + 4 * i), now=t + 4 * i)
        confirm(a, camera=f"C{i}", t=t + 4 * i, span=0.0)
    a.tick(force=True, now=t + 60)
    assert len(out) >= 2
    assert all(max(h_t for h_t in [ev.t]) - ev.t <= CFG.max_episode_seconds for ev in out)
    first = out[0]
    assert len(first.hits) <= CFG.max_episode_seconds // 4 + 1


def test_reopen_guard_covers_the_lowered_t_path():
    """Camera B (fd 1002) opens after A (fd 1000) closed and dragged t to 1000:
    the second episode must be refused, not emitted as a duplicate (t, species)."""
    out = []
    a = asm(out)
    a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=1000.0)
    a.observe(**entry("Blue Jay", "B", 1, 1002.0), now=1001.0)
    confirm(a)
    a.tick(force=True, now=1001.0)
    assert len(out) == 1 and out[0].t == 1000.0
    # BirdNET-Go's buffer still carries both entries a few seconds later
    a.observe(**entry("Blue Jay", "B", 1, 1002.0), now=1004.0)
    a.observe(**entry("Blue Jay", "A", 1, 1000.0), now=1004.0)
    a.tick(force=True, now=1010.0)
    assert len(out) == 1
    assert a.reopen_refused >= 1


def test_soundlevel_snapshot_goes_stale():
    import time
    from birdnet_loc.ingest import SoundLevels
    sm = SourceMap(); sm.learn_hash("rtsp_x", "Doorbell")
    sl = SoundLevels(sm, stale_seconds=20.0)
    sl._by_hash["rtsp_x"] = (time.time() - 5, {"1.0_kHz": {"max_db": -50}})
    assert sl.snapshot_for("Doorbell") is not None
    sl._by_hash["rtsp_x"] = (time.time() - 25, {"1.0_kHz": {"max_db": -50}})
    assert sl.snapshot_for("Doorbell") is None


def test_unknown_source_warns_once(caplog):
    import logging
    from birdnet_loc.bands import BandTable
    from birdnet_loc.ingest import Ingestor
    ing = Ingestor(CFG, BandTable({}, (2500, 6300)), lambda e: None)
    with caplog.at_level(logging.WARNING, logger="birdnet_loc.ingest"):
        for _ in range(5):
            ing.note_unknown_source("Garage")
    assert sum("unknown camera name" in r.getMessage() for r in caplog.records) == 1
    assert ing.unknown_sources == {"Garage": 5}
