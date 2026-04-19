# FOCtave

**Convert stereo e-stim audio into 4-phase restim funscripts for FOC-Stim.**

FOCtave takes a traditional stereo e-stim audio file (WAV / FLAC / MP3) and
produces the five funscripts that [restim](https://github.com/diglet48/restim)
auto-detects when running in FOC-Stim 4-phase mode:

```
<name>.e1.funscript   -> intensity A  \
<name>.e2.funscript   -> intensity B  /  (pair 1, driven by L envelope)
<name>.e3.funscript   -> intensity C  \
<name>.e4.funscript   -> intensity D  /  (pair 2, driven by R envelope)
<name>.volume.funscript -> volume
```

The electrode pair duplication preserves the original stereo topology on a
4-electrode [FOC-Stim](https://github.com/diglet48/FOC-Stim) box: `(e1, e2)`
act as one bipolar terminal and `(e3, e4)` act as the other, so the track
feels the same way it would on a traditional two-channel stimulator - while
also gaining free cross-coupling between pairs thanks to FOC-Stim's any-to-any
routing.

---

## Quick start

```bash
pip install -r requirements.txt
python foctave.py path/to/your_track.wav
```

That's it - five `.funscript` files appear next to the audio, and restim
will auto-detect them when you point it at the matching media file.

MP3 / M4A / OGG inputs work if `ffmpeg` is on your `PATH`.

---

## How it works

1. Load the stereo audio.
2. Take the amplitude envelope of each channel:
   `|signal|` -> 20 Hz low-pass.
3. Compress the envelope with a gentle gamma curve (default cube root) so
   quiet passages stay feel-able without flattening peaks.
4. Normalize to 0-100 using a high percentile (not the absolute peak) so a
   single loud transient doesn't squash the rest of the track.
5. Downsample to ~30 Hz (one action every 33 ms) and dedupe consecutive
   identical values.
6. Duplicate L -> `e1` + `e2`, R -> `e3` + `e4`, RMS -> `volume`.

Defaults are empirically tuned against real-world 4-phase content to
produce a close match to established stereo-to-4-phase conversion styles.

---

## Tuning knobs

| Flag | Default | What it does |
|---|---|---|
| `--rate` | `30` Hz | Funscript sample rate |
| `--smooth` | `20` Hz | Envelope low-pass cutoff |
| `--gamma` | `0.33` | Compression curve exponent (1.0 = linear, 0.5 = sqrt, 0.33 = cube root) |
| `--percentile` | `85` | Normalization percentile (100 = peak) |
| `--attack-ms` | `0` | Asymmetric attack time (try 10-30 for musical feel) |
| `--release-ms` | `0` | Asymmetric release time (try 80-200 for musical feel) |
| `--floor` | `0.0` | Minimum intensity 0-1 (try 0.05-0.10 for comfort) |
| `--volume-ramp` | `0.0` | Additive ramp on the volume channel in %/minute |

### Recommended "comfort" preset

```bash
python foctave.py input.wav --attack-ms 15 --release-ms 120 --floor 0.05
```

- **Attack/release** gives transients punch while avoiding choppy cutoff.
- **Floor** ensures quiet moments never drop to zero, which can feel like
  the connection dropped.

### Recommended "long-session" preset

```bash
python foctave.py input.wav --attack-ms 15 --release-ms 120 --floor 0.05 --volume-ramp 0.5
```

Adds a gradual 0.5%/min ramp on the volume channel (matches the restim wiki's
suggestion for sustained sessions).

---

## Output format

FOCtave writes the minimal funscript JSON used by restim's auto-detect:

```json
{"actions": [{"pos": 72, "at": 0}, {"pos": 74, "at": 33}, ...]}
```

`pos` is `0-100` intensity; `at` is milliseconds from start.

---

## Requirements

- Python 3.11+ (older may work; tested on 3.11)
- `numpy`, `scipy`, `soundfile` (see `requirements.txt`)
- `ffmpeg` on `PATH` for non-WAV/FLAC inputs

---

## Credits

- [restim](https://github.com/diglet48/restim) by diglet48 - the e-stim
  control software this tool targets.
- [FOC-Stim](https://github.com/diglet48/FOC-Stim) by diglet48 - the
  hardware this tool is designed for.
- Inspired by reverse-engineering the stereo-to-4-phase conversion style
  used by FunBelgium's published scripts.

---

## License

MIT - see [LICENSE](LICENSE).
