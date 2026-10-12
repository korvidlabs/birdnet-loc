import io
import json

import pytest

from birdnet_loc import bands_tool
from birdnet_loc.bands import load_bands

TABLE = "# hand-made\ndefault: [2500, 6300]\nspecies:\n  Blue Jay: [1600, 3100]   # keep this comment\n  Rock Pigeon: [250, 500]\n"


@pytest.fixture
def bands(tmp_path):
    p = tmp_path / "bands.yaml"
    p.write_text(TABLE)
    return str(p)


def run(*argv):
    out = io.StringIO()
    code = bands_tool.main(list(argv), out=out)
    return code, out.getvalue()


def test_check_lists_every_species_with_its_keys(bands):
    code, text = run("--bands", bands, "check")
    assert code == 0
    assert "Blue Jay: [1600, 3100] -> 1.6_kHz, 2.0_kHz, 2.5_kHz" in text
    assert "Rock Pigeon: [250, 500] -> 250.0_Hz, 315.0_Hz, 400.0_Hz, 500.0_Hz" in text
    assert "2 species, 0 with problems" in text


def test_check_flags_an_inverted_range(tmp_path):
    p = tmp_path / "bands.yaml"
    p.write_text("species:\n  Upside Down: [5000, 2500]\n")
    code, text = run("--bands", str(p), "check")
    assert code == 1
    assert "Upside Down" in text and "lo must be below hi" in text


def test_keys_explains_a_narrow_range_falls_to_the_nearest_centre():
    code, text = run("keys", "2600", "2700")
    assert code == 0
    assert "-> 2.5_kHz" in text and "nearest band used" in text


def test_keys_rejects_nonsense():
    code, text = run("keys", "0", "-5")
    assert code == 1 and "positive" in text


def test_add_appends_without_disturbing_comments(bands):
    code, text = run("--bands", bands, "add", "House Finch", "2500", "5000")
    assert code == 0
    assert "House Finch: [2500, 5000] -> 2.5_kHz, 3.1_kHz, 4.0_kHz, 5.0_kHz" in text
    raw = open(bands).read()
    assert "# hand-made" in raw and "# keep this comment" in raw
    assert raw.endswith("  House Finch: [2500, 5000]\n")
    assert load_bands(bands).band_for("House Finch") == (2500.0, 5000.0)


def test_add_quotes_a_name_yaml_would_misread(bands):
    code, _ = run("--bands", bands, "add", "Odd: Bird", "1000", "2000")
    assert code == 0
    assert load_bands(bands).band_for("Odd: Bird") == (1000.0, 2000.0)


def test_add_refuses_a_duplicate_and_a_bad_range(bands):
    code, text = run("--bands", bands, "add", "Blue Jay", "1000", "2000")
    assert code == 1 and "already has" in text
    code, text = run("--bands", bands, "add", "New Bird", "3000", "1000")
    assert code == 1 and "lo must be below hi" in text
    assert "New Bird" not in open(bands).read()


def test_add_creates_the_file_when_absent(tmp_path):
    p = tmp_path / "fresh.yaml"
    code, _ = run("--bands", str(p), "add", "Blue Jay", "1600", "3100")
    assert code == 0
    t = load_bands(str(p))
    assert t.default == (2500.0, 6300.0) and t.band_for("Blue Jay") == (1600.0, 3100.0)


SUMMARY = [
    {"common_name": "Blue Jay", "count": 5126, "last_heard": "2026-10-11T18:23:18-06:00"},
    {"common_name": "Red-breasted Nuthatch", "count": 1671, "last_heard": "2026-10-11T18:05:37-06:00"},
    {"common_name": "Spotted Towhee", "count": 2, "last_heard": "2026-09-20T07:00:00-06:00"},
    {"common_name": "Rock Pigeon", "count": 90, "last_heard": "2026-10-01T07:00:00-06:00"},
]


def test_missing_reports_unlisted_species_by_count_without_network(bands):
    calls = []

    def fetch(url):
        calls.append(url)
        return json.dumps(SUMMARY).encode()

    out = io.StringIO()
    code = bands_tool.cmd_missing(bands, "http://birdnet:8080/", 1, out, fetch=fetch)
    text = out.getvalue()
    assert code == 0
    assert calls == ["http://birdnet:8080/api/v2/analytics/species/summary"]
    assert "2 species heard" in text
    assert text.index("Red-breasted Nuthatch: 1671 detections, last 2026-10-11") < text.index("Spotted Towhee: 2 detections")
    assert "Blue Jay" not in text.split("\n", 2)[2] and "Rock Pigeon" not in text.split("\n", 2)[2]


def test_missing_honours_min_count(bands):
    out = io.StringIO()
    bands_tool.cmd_missing(bands, "http://b", 10, out, fetch=lambda u: json.dumps(SUMMARY).encode())
    assert "Spotted Towhee" not in out.getvalue() and "Red-breasted Nuthatch" in out.getvalue()


def test_missing_with_nothing_missing_says_so(bands):
    out = io.StringIO()
    only_known = [r for r in SUMMARY if r["common_name"] in ("Blue Jay", "Rock Pigeon")]
    code = bands_tool.cmd_missing(bands, "http://b", 1, out, fetch=lambda u: json.dumps(only_known).encode())
    assert code == 0 and "every species" in out.getvalue()
