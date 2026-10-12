"""estimate() against synthetic episodes.

Two signals drive a blob now. hitCount and relative band level set the *weight*
of each camera; the absolute band level, against the species' self-calibrated
reference, sets the *range* along that camera's bearing.
"""

import math
import pathlib

import pytest

from birdnet_loc.cameras import Camera, CameraSet, load_cameras
from birdnet_loc.config import Config
from birdnet_loc.estimate import Event, Hit, estimate, range_for

SCALE = 0.03140  # metres per pixel for the synthetic camera sets below
CFG = Config()
FULL = int(CFG.hits_full)   # a hitCount that scores hits_i = 1.0
REPO = pathlib.Path(__file__).resolve().parents[1]


def cam(name, x_px, y_px, bearing, fov=180.0):
    return Camera(name=name, x_px=x_px, y_px=y_px, bearing=bearing, fov_deg=fov, scale_m_per_px=SCALE)


def cams(*items):
    return CameraSet(
        cameras={c.name: c for c in items},
        image="map.png",
        image_size=(1071, 1008),
        scale_m_per_px=SCALE,
    )


def real_cameras():
    return load_cameras(str(REPO / "example" / "map" / "cameras.yaml"))


def event(*hits, species="Blue Jay", t=1000.0):
    return Event(species=species, sci_name="Cyanocitta cristata", t=t, hits=tuple(hits))


def px(metres):
    return metres / SCALE


def ref(db):
    """A ref_db_fn that reports the same calibrated reference for any species."""
    return lambda species: db


# -- Case 1: one camera -> blob at its range, in its field ------------


def test_single_camera_blob_sits_in_the_field_at_the_fallback_range():
    east = cam("East", 500, 500, 90)  # faces east: +x
    blob = estimate(event(Hit("East", FULL)), cams(east), CFG)

    assert blob is not None
    # no band level and no calibration -> FALLBACK_RANGE_M along the bearing
    assert blob.x_px == pytest.approx(500 + px(CFG.fallback_range_m))
    assert blob.y_px == pytest.approx(500)
    # radius grows with range: half of it, floored at MIN_RADIUS_M
    assert blob.radius_px == pytest.approx(px(max(CFG.min_radius_m, CFG.fallback_range_m / 2)))
    assert east.in_field(blob.x_px * SCALE, blob.y_px * SCALE)
    assert blob.cameras[0]["name"] == "East"
    assert blob.cameras[0]["range_m"] == pytest.approx(CFG.fallback_range_m)
    assert blob.cameras[0]["ref_db"] is None
    assert blob.clamped is False and blob.degraded is False


# -- Case 2: two opposite cameras, equal weight -> midpoint -----------


def test_two_opposed_equal_cameras_land_on_the_midpoint():
    west_facing = cam("A", 300, 500, 90)   # at x=300 looking east
    east_facing = cam("B", 900, 500, 270)  # at x=900 looking west
    blob = estimate(event(Hit("A", FULL), Hit("B", FULL)), cams(west_facing, east_facing), CFG)

    assert blob is not None
    assert blob.x_px == pytest.approx(600.0)
    assert blob.y_px == pytest.approx(500.0)


# -- Case 3: the louder camera pulls the estimate --------------------


def test_louder_camera_pulls_the_estimate_toward_it():
    a = cam("A", 300, 500, 90)
    b = cam("B", 900, 500, 270)
    quiet = estimate(event(Hit("A", FULL, -70.0), Hit("B", FULL, -70.0)), cams(a, b), CFG)
    louder_a = estimate(event(Hit("A", FULL, -55.0), Hit("B", FULL, -70.0)), cams(a, b), CFG)

    assert quiet.x_px == pytest.approx(600.0)
    assert louder_a.x_px < quiet.x_px          # pulled toward A, which sits at x=300
    assert louder_a.x_px > 300.0               # but not all the way onto the camera


# -- the range model -------------------------------------------------------


def test_range_halves_the_distance_every_six_db_louder():
    assert range_for(-50.0, -50.0, CFG) == pytest.approx(CFG.ref_range_m)
    # 6 dB quieter than the reference is twice as far
    assert range_for(-56.0, -50.0, CFG) == pytest.approx(CFG.ref_range_m * 2, rel=0.01)   # 6 dB is 1.995x, not exactly 2x
    assert range_for(-62.0, -50.0, CFG) == pytest.approx(CFG.ref_range_m * 4, rel=0.02)   # 6 dB is 1.995x, not exactly 2x


def test_range_is_clamped_at_both_ends():
    assert range_for(-10.0, -50.0, CFG) == pytest.approx(CFG.min_range_m)   # deafening
    assert range_for(-120.0, -50.0, CFG) == pytest.approx(CFG.max_range_m)  # inaudible


def test_uncalibrated_species_falls_back_to_a_fixed_range():
    east = cam("East", 500, 500, 90)
    # a level is present, but the species has no reference yet
    blob = estimate(event(Hit("East", FULL, -55.0)), cams(east), CFG, lambda s: None)
    assert blob.cameras[0]["range_m"] == pytest.approx(CFG.fallback_range_m)
    assert blob.x_px == pytest.approx(500 + px(CFG.fallback_range_m))


def test_a_camera_with_no_level_falls_back_even_when_calibrated():
    east = cam("East", 500, 500, 90)
    blob = estimate(event(Hit("East", FULL, None)), cams(east), CFG, ref(-50.0))
    assert blob.cameras[0]["range_m"] == pytest.approx(CFG.fallback_range_m)


def test_louder_means_nearer_the_camera():
    east = cam("East", 500, 500, 90)
    cs = cams(east)
    loud = estimate(event(Hit("East", FULL, -50.0)), cs, CFG, ref(-50.0))
    quiet = estimate(event(Hit("East", FULL, -62.0)), cs, CFG, ref(-50.0))

    assert loud.cameras[0]["range_m"] < quiet.cameras[0]["range_m"]
    assert loud.cameras[0]["range_m"] == pytest.approx(CFG.ref_range_m)
    assert loud.x_px - 500 < quiet.x_px - 500       # the loud blob sits nearer
    assert loud.radius_px <= quiet.radius_px        # and is no less certain
    assert loud.cameras[0]["ref_db"] == pytest.approx(-50.0)


def test_range_places_two_cameras_points_not_their_positions():
    a = cam("A", 300, 500, 90)     # faces east
    b = cam("B", 900, 500, 270)    # faces west
    # A hears it 6 dB louder: A's point is 3 m out, B's is 6 m out
    blob = estimate(event(Hit("A", FULL, -50.0), Hit("B", FULL, -56.0)), cams(a, b), CFG,
                    ref(-50.0))
    by_name = {c["name"]: c for c in blob.cameras}
    assert by_name["A"]["range_m"] == pytest.approx(3.0, rel=0.01)   # 6 dB is 1.995x, not exactly 2x
    assert by_name["B"]["range_m"] == pytest.approx(6.0, rel=0.01)   # 6 dB is 1.995x, not exactly 2x
    # A's point x = 300 + px(3), B's = 900 - px(6); A also carries twice the
    # band weight, so the centre sits nearer A's point than B's
    a_point, b_point = 300 + px(3.0), 900 - px(6.0)
    assert a_point < blob.x_px < b_point
    assert abs(blob.x_px - a_point) < abs(blob.x_px - b_point)


# -- direction term --------------------------------------------------------


def test_camera_facing_away_gets_behind_weight_with_three_contributors():
    a = cam("A", 300, 500, 90)      # faces east, toward the others
    b = cam("B", 320, 560, 90)      # faces east, toward the others
    away = cam("C", 900, 500, 90)   # faces east: the other two are behind it
    blob = estimate(event(Hit("A", FULL), Hit("B", FULL), Hit("C", FULL)),
                    cams(a, b, away), CFG)

    by_name = {c["name"]: c for c in blob.cameras}
    assert by_name["A"]["dir"] == 1.0
    assert by_name["B"]["dir"] == 1.0
    assert by_name["C"]["dir"] == CFG.behind_weight
    assert by_name["C"]["weight"] == pytest.approx(1.0 * CFG.behind_weight)


def test_two_opposed_cameras_never_penalise_each_other():
    """Finding E: with two contributors the bird is between them, so neither
    is 'behind'. Back Yard South and Side Yard South are the real opposed pair."""
    cs = real_cameras()
    both = estimate(event(Hit("Back Yard South", FULL), Hit("Side Yard South", FULL)), cs, CFG)
    one = estimate(event(Hit("Back Yard South", FULL)), cs, CFG)
    other = estimate(event(Hit("Side Yard South", FULL)), cs, CFG)

    assert {c["dir"] for c in both.cameras} == {1.0}
    assert both.confidence >= one.confidence
    assert both.confidence >= other.confidence


def test_a_single_contributor_is_never_behind_itself():
    away = cam("C", 900, 500, 90)
    blob = estimate(event(Hit("C", FULL)), cams(away), CFG)
    assert blob.cameras[0]["dir"] == 1.0


# -- confidence ------------------------------------------------------------


def test_confidence_rises_with_more_contributing_cameras():
    a, b, c = cam("A", 300, 500, 90), cam("B", 900, 500, 270), cam("C", 600, 200, 180)
    cs = cams(a, b, c)
    one = estimate(event(Hit("A", FULL)), cs, CFG)
    two = estimate(event(Hit("A", FULL), Hit("B", FULL)), cs, CFG)
    three = estimate(event(Hit("A", FULL), Hit("B", FULL), Hit("C", FULL)), cs, CFG)

    assert one.confidence < two.confidence < three.confidence
    assert 0.0 < one.confidence < 1.0 and three.confidence < 1.0
    # confidence = 1 - 1/(1 + sum w); one camera at hits 1.0, band 1.0, dir 1.0
    assert one.confidence == pytest.approx(0.5)


# -- radius clamps ---------------------------------------------------------


def test_radius_is_floored_for_near_coincident_cameras():
    a = cam("A", 500, 500, 90)
    b = cam("B", 502, 500, 90)
    blob = estimate(event(Hit("A", FULL), Hit("B", FULL)), cams(a, b), CFG)
    assert blob.radius_px == pytest.approx(px(CFG.min_radius_m))


def test_radius_is_ceilinged_for_far_apart_cameras():
    a = cam("A", 0, 500, 90)
    b = cam("B", 3000, 500, 270)
    cs = CameraSet(cameras={a.name: a, b.name: b}, image="map.png",
                   image_size=(4000, 1008), scale_m_per_px=SCALE)
    blob = estimate(event(Hit("A", FULL), Hit("B", FULL)), cs, CFG)
    assert blob.radius_px == pytest.approx(px(CFG.max_radius_m))


# -- off-image centres (finding B) -----------------------------------------


def test_a_centre_past_the_edge_is_kept_and_flagged():
    """An east-facing camera's bird really is over the street: the estimate is
    NOT moved back onto the camera, it is flagged as extending beyond the map."""
    edge = cam("Edge", 1050, 500, 90)      # near the east edge, facing off-map
    blob = estimate(event(Hit("Edge", FULL, -110.0)), cams(edge), CFG, ref(-50.0))

    assert blob.clamped is True
    assert blob.x_px > 1050                # still east of the camera
    assert blob.x_px + blob.radius_px > 1071.0


def test_a_centre_well_inside_is_not_flagged():
    mid = cam("Mid", 500, 500, 90)
    blob = estimate(event(Hit("Mid", FULL)), cams(mid), CFG)
    assert blob.clamped is False


def test_a_blob_wider_than_the_map_is_flagged_not_moved():
    a = cam("A", 0, 500, 90)
    b = cam("B", 3000, 500, 270)
    blob = estimate(event(Hit("A", FULL), Hit("B", FULL)), cams(a, b), CFG)
    assert blob.clamped is True
    assert blob.x_px == pytest.approx(1500.0)   # the true centroid, untouched


# -- unknown camera names (finding H) --------------------------------------


def test_unknown_camera_names_are_dropped():
    a = cam("A", 500, 500, 90)
    assert estimate(event(Hit("Nowhere", FULL)), cams(a), CFG) is None


def test_losing_a_camera_to_an_unknown_name_is_degraded_not_confident():
    a = cam("A", 300, 500, 90)
    cs = cams(a)
    lone = estimate(event(Hit("A", FULL)), cs, CFG)
    lost = estimate(event(Hit("A", FULL), Hit("Not On The Map", FULL)), cs, CFG)

    assert lone.degraded is False
    assert lost.degraded is True
    assert lost.n_cameras == 1
    assert lost.confidence <= lone.confidence
    assert lost.to_dict()["degraded"] is True


# -- band weighting (finding G) --------------------------------------------


def test_band_factor_is_relative_to_the_quietest_camera():
    a, b = cam("A", 300, 500, 90), cam("B", 900, 500, 270)
    blob = estimate(event(Hit("A", FULL, -50.0), Hit("B", FULL, -70.0)), cams(a, b), CFG)
    by_name = {c["name"]: c for c in blob.cameras}
    assert by_name["B"]["weight"] == pytest.approx(1.0)
    assert by_name["A"]["weight"] == pytest.approx(10.0)   # capped, would be 10^1


def test_band_factor_is_capped():
    a, b = cam("A", 300, 500, 90), cam("B", 900, 500, 270)
    blob = estimate(event(Hit("A", FULL, -10.0), Hit("B", FULL, -90.0)), cams(a, b), CFG)
    by_name = {c["name"]: c for c in blob.cameras}
    assert by_name["A"]["weight"] == pytest.approx(10.0)


def test_band_weighting_is_skipped_unless_every_camera_has_a_level():
    """One measured camera plus one unmeasured is not a comparison: treating the
    unmeasured one as the quietest invents a level it never reported."""
    a, b = cam("A", 300, 500, 90), cam("B", 900, 500, 270)
    blob = estimate(event(Hit("A", FULL, -50.0), Hit("B", FULL, None)), cams(a, b), CFG)
    assert all(c["weight"] == pytest.approx(1.0) for c in blob.cameras)


def test_missing_band_data_weighs_as_one():
    a, b = cam("A", 300, 500, 90), cam("B", 900, 500, 270)
    blob = estimate(event(Hit("A", FULL), Hit("B", FULL)), cams(a, b), CFG)
    assert all(c["band_db"] is None and c["weight"] == pytest.approx(1.0) for c in blob.cameras)


# -- the hits term ---------------------------------------------------------


def test_hit_count_scales_the_weight_up_to_hits_full():
    a, b = cam("A", 300, 500, 90), cam("B", 900, 500, 270)
    blob = estimate(event(Hit("A", 1), Hit("B", FULL)), cams(a, b), CFG)
    by_name = {c["name"]: c for c in blob.cameras}
    assert by_name["A"]["weight"] == pytest.approx(1.0 / CFG.hits_full, abs=1e-4)
    assert by_name["B"]["weight"] == pytest.approx(1.0)
    assert by_name["A"]["hits"] == 1
    # and more hits than HITS_FULL never buys more than a full weight
    over = estimate(event(Hit("A", 99), Hit("B", FULL)), cams(a, b), CFG)
    assert {c["name"]: c["weight"] for c in over.cameras}["A"] == pytest.approx(1.0)


def test_a_zero_hitcount_still_counts_as_one_window():
    from birdnet_loc.estimate import hits_factor
    assert hits_factor(0, CFG.hits_full) == pytest.approx(1.0 / CFG.hits_full)
    assert hits_factor(1, CFG.hits_full) == pytest.approx(1.0 / CFG.hits_full)


def test_more_hits_pull_the_estimate_toward_that_camera():
    a, b = cam("A", 300, 500, 90), cam("B", 900, 500, 270)
    even = estimate(event(Hit("A", FULL), Hit("B", FULL)), cams(a, b), CFG)
    a_loud = estimate(event(Hit("A", FULL), Hit("B", 1)), cams(a, b), CFG)
    assert even.x_px == pytest.approx(600.0)
    assert a_loud.x_px < even.x_px


def test_blob_reports_the_camera_count_and_a_null_confidence_until_confirmed():
    a, b = cam("A", 300, 500, 90), cam("B", 900, 500, 270)
    d = estimate(event(Hit("A", FULL), Hit("B", FULL, None, 0.82)), cams(a, b), CFG).to_dict()
    assert d["n_cameras"] == 2
    by_name = {c["name"]: c for c in d["cameras"]}
    assert by_name["A"]["conf"] is None and by_name["B"]["conf"] == 0.82
    assert "range_m" in by_name["A"] and "ref_db" in by_name["A"]


def test_bearing_unit_vector_is_compass_north_up():
    north = cam("N", 0, 0, 0)
    assert north.unit[0] == pytest.approx(0.0, abs=1e-12)
    assert north.unit[1] == pytest.approx(-1.0)
    east = cam("E", 0, 0, 90)
    assert east.unit == pytest.approx((1.0, 0.0), abs=1e-12)
    assert math.isclose(math.hypot(*cam("X", 0, 0, 232).unit), 1.0)


def test_the_blob_carries_the_confirmation_that_let_it_be_emitted():
    """confirmed_conf / confirmed_by come from the one Hit the gate confirmed;
    the other cameras carry conf=None by construction."""
    cams = load_cameras(str(REPO / "example" / "map" / "cameras.yaml"))
    names = list(cams.cameras)[:2]
    ev = Event(species="House Finch", sci_name="Haemorhous mexicanus", t=1000.0,
               hits=(Hit(names[0], FULL), Hit(names[1], FULL, conf=0.83)))
    blob = estimate(ev, cams, CFG, lambda s: None)
    assert blob.confirmed_conf == 0.83
    assert blob.confirmed_by == names[1]
    assert blob.to_dict()["confirmed_by"] == names[1]


def test_an_unconfirmed_event_would_carry_no_confirmation():
    """Unreachable through the gate; asserted so the field never silently
    defaults to a number the page would present as evidence."""
    cams = load_cameras(str(REPO / "example" / "map" / "cameras.yaml"))
    ev = Event(species="Sora", sci_name="Porzana carolina", t=1000.0,
               hits=(Hit(list(cams.cameras)[0], FULL),))
    blob = estimate(ev, cams, CFG, lambda s: None)
    assert blob.confirmed_conf is None and blob.confirmed_by is None
