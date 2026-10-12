# Filling in `bands.yaml`

`bands.yaml` tells birdnet-loc which part of the spectrum each species sings in. That matters
because distance is read from loudness: for every camera that heard a bird, the tool takes the
loudest 1/3-octave level *inside the species' band* and converts it to a range. With the right band
the level tracks the bird. With the wrong one it tracks traffic, wind or a different bird, and the
species gets placed too far away or too close, on every camera, every time.

Species not in the file use `default` (2.5-6.3 kHz, where most small songbirds sit). That is a
tolerable guess for a warbler and a poor one for a dove or an owl. Add the species you actually
hear, starting with the loudest and most frequent.

## The file

```yaml
default: [2500, 6300]
species:
  Blue Jay: [1600, 3100]
  Mourning Dove: [400, 630]
```

Keys are BirdNET-Go common names, spelled exactly as they appear on its detections page (the
`missing` command below prints them in that form). Values are `[low_hz, high_hz]`. Comments are
fine; the tool never rewrites the file, it only appends.

## The grid

BirdNET-Go reports sound levels in 30 standard 1/3-octave bands, not in free-form frequencies.
A range selects every band whose *centre* falls inside it:

| centre (Hz) | 25 | 31.5 | 40 | 50 | 63 | 80 | 100 | 125 | 160 | 200 | 250 | 315 | 400 | 500 | 630 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **centre (Hz)** | **800** | **1000** | **1250** | **1600** | **2000** | **2500** | **3150** | **4000** | **5000** | **6300** | **8000** | **10000** | **12500** | **16000** | **20000** |

Two consequences:

- **Edges must enclose a centre.** `[1600, 3100]` selects 1600, 2000 and 2500 Hz but *not* the
  3150 Hz band, because 3100 < 3150. Write `[1600, 3200]` if you want that band in. `keys` shows
  exactly what a range selects before you commit to it.
- **A range narrower than the grid still works.** If no centre falls inside, the nearest band is
  used, so `[2600, 2700]` means the 2500 Hz band. The tool says so when this happens.

Aim for two to five bands. One band is fragile (a slightly higher-pitched individual falls
outside it); more than five starts to admit noise from the rest of the spectrum.

## Finding a species' range

Any of these is enough. You want the frequency span where the *loudest* part of the song or call
lives, not the faint harmonics above it.

1. **BirdNET-Go's own spectrogram.** Open a detection of the species in the BirdNET-Go web UI.
   The spectrogram's vertical axis is frequency; read the bottom and top of the dark band where
   the call is. Three or four detections from different days are plenty.
2. **A recording site with spectrograms.** xeno-canto and the Macaulay Library draw one for
   every recording.
3. **A field guide or species account.** Many give the song's frequency range in kHz. Birds of the
   World and the Cornell "All About Birds" sound pages are the usual sources.

Rules of thumb while you look:

- Doves, pigeons, owls, grouse: low, usually 200-800 Hz.
- Corvids, woodpeckers, thrushes, nuthatches: mid, roughly 1-4 kHz.
- Finches, sparrows, chickadees, warblers, wrens: high, roughly 2.5-8 kHz.
- Hawks and gulls scream broadly; a 1.5-4 kHz band catches the bulk of it.

When a species has a song and a call in different ranges, choose the one BirdNET-Go actually
detects in your yard. Its detection page shows which it was.

## Using the tool

```sh
uv run bands-tool check                                    # validate; show the bands each range selects
uv run bands-tool missing --birdnet-url http://host:8080   # species heard that have no entry, most frequent first
uv run bands-tool keys 1600 3200                           # try a range before writing it
uv run bands-tool add "Blue Jay" 1600 3200                 # append it
```

`missing` reads BirdNET-Go's species summary and reports; it never guesses a band. `--min-count 20`
hides species heard only a handful of times, which are often misidentifications anyway. `add`
refuses a duplicate, an inverted range and anything outside the grid, and reads the file back after
writing so a typo cannot leave the table unreadable. Every command exits non-zero on a problem.

Pass `--bands PATH` (or set `BANDS_YAML`) to work on a copy; the service reads `BANDS_YAML` too, so
a deployment can keep its table outside the repo.

## After changing a band

Restart the service (`docker compose up -d --build` when the file is baked into the image; the
Dockerfile copies it). Changing a species' band also changes what its loudness samples mean: the
per-species calibration (the 95th percentile of levels it has reached, over a rolling reservoir of
`CALIB_MAX_N` = 500 samples) was measured in the old band and re-learns from new samples. Expect
that species' positions to shift for a day or two after an edit, then settle.

## Worked example

`missing` on a fresh install in the Colorado Front Range:

```
# 8 species heard by http://localhost:9520 without a band (default [2500, 6300] applies).
#   Red-breasted Nuthatch: 1671 detections, last 2026-10-11
#   Red-tailed Hawk: 171 detections, last 2026-10-11
#   Say's Phoebe: 125 detections, last 2026-10-11
```

The nuthatch is a tin-horn "yank" that a spectrogram shows concentrated around 2-4 kHz:

```
$ uv run bands-tool keys 2000 4000
[2000, 4000] -> 2.0_kHz, 2.5_kHz, 3.1_kHz, 4.0_kHz
$ uv run bands-tool add "Red-breasted Nuthatch" 2000 4000
Red-breasted Nuthatch: [2000, 4000] -> 2.0_kHz, 2.5_kHz, 3.1_kHz, 4.0_kHz
```

Four bands, centred on the call, done. Rebuild, and the species drops off `missing`.
