"""Store: add / dedup / prune / load_recent, and the calibration reservoir."""

import time

import pytest

from birdnet_loc.estimate import Blob
from birdnet_loc.store import Calibration, Store, percentile


def blob(t, species="Blue Jay", **kw):
    return Blob(
        species=species,
        sci_name=kw.pop("sci_name", "Cyanocitta cristata"),
        t=t,
        x_px=kw.pop("x_px", 500.0),
        y_px=kw.pop("y_px", 400.0),
        radius_px=kw.pop("radius_px", 120.0),
        confidence=kw.pop("confidence", 0.5),
        cameras=kw.pop("cameras", [{"name": "A", "hits": 3, "range_m": 6.0}]),
        **kw,
    )


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "birdnet-loc.db"), history_seconds=3600.0)
    yield s
    s.close()


# -- add / dedup -----------------------------------------------------------


def test_add_stores_and_reports_success(store):
    now = time.time()
    assert store.add(blob(now)) is True
    assert len(store) == 1
    assert store.since(0)[0].species == "Blue Jay"


def test_the_same_t_and_species_is_rejected_not_duplicated(store):
    now = time.time()
    assert store.add(blob(now)) is True
    assert store.add(blob(now)) is False
    assert len(store) == 1


def test_the_same_t_with_a_different_species_is_a_different_blob(store):
    now = time.time()
    assert store.add(blob(now, species="Blue Jay")) is True
    assert store.add(blob(now, species="House Finch")) is True
    assert len(store) == 2


def test_flags_survive_a_round_trip(store):
    now = time.time()
    store.add(blob(now, clamped=True, degraded=True))
    got = store.since(0)[0]
    assert got.clamped is True and got.degraded is True
    assert got.cameras[0]["range_m"] == 6.0


# -- pruning ---------------------------------------------------------------


def test_blobs_older_than_the_window_are_pruned(store):
    now = time.time()
    store.add(blob(now - 7200, species="Old"))
    store.add(blob(now, species="New"))
    assert [b.species for b in store.since(0)] == ["New"]


def test_pruning_is_robust_to_out_of_order_arrival(store):
    """An episode's t is its earliest firstDetected, so a long episode can be
    stored after a shorter one that started later. Popping from the left until
    the head is fresh would strand the old blob behind the new one forever."""
    now = time.time()
    store.add(blob(now, species="Fresh"))          # newest arrives first
    store.add(blob(now - 7200, species="Ancient"))  # then an old one
    store.add(blob(now - 10, species="Recent"))

    kept = {b.species for b in store.since(0)}
    assert kept == {"Fresh", "Recent"}
    assert len(store) == 2


def test_since_filters_by_time_and_species(store):
    now = time.time()
    store.add(blob(now - 600, species="Blue Jay"))
    store.add(blob(now - 10, species="Blue Jay"))
    store.add(blob(now - 10, species="House Finch"))

    assert len(store.since(now - 60)) == 2
    assert [b.species for b in store.since(0, "House Finch")] == ["House Finch"]


def test_since_returns_ascending_t(store):
    now = time.time()
    for t in (now - 5, now - 300, now - 60):
        store.add(blob(t))
    ts = [b.t for b in store.since(0)]
    assert ts == sorted(ts)


# -- load_recent -----------------------------------------------------------


def test_load_recent_repopulates_the_ring_from_sqlite(tmp_path):
    path = str(tmp_path / "birdnet-loc.db")
    now = time.time()
    first = Store(path, history_seconds=3600.0)
    first.add(blob(now - 10, species="Blue Jay", clamped=True))
    first.add(blob(now - 7200, species="Too Old"))
    first.close()

    second = Store(path, history_seconds=3600.0)
    assert second.load_recent() == 1
    rows = second.since(0)
    assert rows[0].species == "Blue Jay" and rows[0].clamped is True
    second.close()


def test_load_recent_handles_out_of_order_rows(tmp_path):
    path = str(tmp_path / "birdnet-loc.db")
    now = time.time()
    first = Store(path, history_seconds=3600.0)
    first.add(blob(now - 5, species="Fresh"))
    first.add(blob(now - 1800, species="Older"))
    first.close()

    second = Store(path, history_seconds=3600.0)
    assert second.load_recent() == 2
    assert [b.t for b in second.since(0)] == sorted(b.t for b in second.since(0))
    second.close()


# -- calibration -----------------------------------------------------------


def test_percentile_is_nearest_rank():
    assert percentile([1.0], 95) == 1.0
    assert percentile([float(i) for i in range(1, 101)], 95) == 95.0
    assert percentile([3.0, 1.0, 2.0], 100) == 3.0


def test_a_species_has_no_reference_until_min_n_samples(store):
    calib = Calibration(store.conn, store.lock, min_n=5, max_n=100)
    for _ in range(4):
        calib.observe("Blue Jay", -60.0)
    assert calib.ref_db("Blue Jay") is None
    assert calib.species_calibrated == 0
    calib.observe("Blue Jay", -60.0)
    assert calib.ref_db("Blue Jay") == pytest.approx(-60.0)
    assert calib.species_calibrated == 1


def test_the_reference_is_the_loud_tail_not_the_average(store):
    calib = Calibration(store.conn, store.lock, min_n=5, max_n=100, pct=95.0)
    for db in [-80.0] * 18 + [-40.0, -40.0]:
        calib.observe("Blue Jay", db)
    # nearest-rank p95 of 20 samples is the 19th, which is the loud tail
    assert calib.ref_db("Blue Jay") == pytest.approx(-40.0)
    assert sum([-80.0] * 18 + [-40.0, -40.0]) / 20 < -70.0    # the mean is not


def test_missing_and_nonfinite_levels_are_ignored(store):
    calib = Calibration(store.conn, store.lock, min_n=1)
    calib.observe("Blue Jay", None)
    calib.observe("Blue Jay", float("inf"))
    calib.observe("", -50.0)
    assert calib.samples("Blue Jay") == 0
    assert calib.ref_db("Blue Jay") is None


def test_the_reservoir_is_bounded_and_persisted(tmp_path):
    path = str(tmp_path / "birdnet-loc.db")
    first = Store(path)
    calib = Calibration(first.conn, first.lock, min_n=5, max_n=50)
    for i in range(120):
        calib.observe("Blue Jay", -100.0 + i)
    assert calib.samples("Blue Jay") == 50
    first.close()

    second = Store(path)
    reloaded = Calibration(second.conn, second.lock, min_n=5, max_n=50)
    assert reloaded.load() == 50            # the DB was trimmed too
    assert reloaded.samples("Blue Jay") == 50
    # the newest values survived, not the oldest
    assert reloaded.ref_db("Blue Jay") > -60.0
    second.close()


def test_species_are_calibrated_independently(store):
    calib = Calibration(store.conn, store.lock, min_n=3)
    for _ in range(3):
        calib.observe("Blue Jay", -50.0)
    calib.observe("House Finch", -70.0)
    assert calib.ref_db("Blue Jay") == pytest.approx(-50.0)
    assert calib.ref_db("House Finch") is None
    assert calib.species_calibrated == 1 and calib.species_seen == 2


def test_the_confirmation_columns_round_trip_through_sqlite(store):
    """Added by the idempotent migration; a blob's gate evidence must survive a
    restart, or the page cannot say what confirmed it."""
    from birdnet_loc.estimate import Blob
    import time as _t
    t = _t.time()
    assert store.add(Blob(species="House Finch", sci_name="Haemorhous mexicanus", t=t,
                          x_px=1.0, y_px=2.0, radius_px=3.0, confidence=0.5,
                          cameras=[{"name": "Doorbell"}],
                          confirmed_conf=0.83, confirmed_by="Doorbell"))
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(blobs)")}
    assert {"confirmed_conf", "confirmed_by"} <= cols

    reopened = Store(store.db_path)
    assert reopened.load_recent() == 1
    b = reopened.since(0.0)[0]
    assert b.confirmed_conf == 0.83 and b.confirmed_by == "Doorbell"
    assert b.to_dict()["confirmed_by"] == "Doorbell"
    reopened.close()
